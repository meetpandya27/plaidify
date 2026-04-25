import Foundation
import XCTest
@testable import PlaidifyLinkKit

final class PlaidifyLinkConnectFlowTests: XCTestCase {
    private func makeOrg(
        site: String = "rbc",
        authStyle: String? = "username_password"
    ) -> PlaidifyOrganization {
        PlaidifyOrganization(
            organizationID: "org_\(site)",
            name: "Test \(site)",
            site: site,
            logoURL: nil,
            primaryColor: nil,
            accentColor: nil,
            secondaryColor: nil,
            hintCopy: nil,
            authStyle: authStyle
        )
    }

    func testHappyPathPickerToCredentialsToConnectingToSuccess() {
        var events: [PlaidifyLinkFlowEvent] = []
        let registry = PlaidifyLinkInstitutionRegistry(
            supportedSites: ["rbc"],
            supportedAuthStyles: ["username_password"]
        )
        let flow = PlaidifyLinkConnectFlow(registry: registry) { events.append($0) }

        let org = makeOrg()
        flow.apply(.selectInstitution(org))
        XCTAssertEqual(flow.state.step, .credentials)
        XCTAssertEqual(flow.state.organization, org)

        flow.apply(.credentialsSubmitted)
        XCTAssertEqual(flow.state.step, .connecting)

        flow.apply(.connectResponded(PlaidifyConnectResponse(
            status: "completed",
            sessionID: nil, mfaType: nil,
            publicToken: "public-1", jobID: "job-1",
            message: nil, errorMessage: nil
        )))
        XCTAssertEqual(flow.state.step, .success)
        XCTAssertEqual(flow.state.publicToken, "public-1")
        XCTAssertTrue(events.contains(.connected(publicToken: "public-1", jobID: "job-1", site: "rbc")))
    }

    func testMFAResponseTransitionsAndIncludesType() {
        let registry = PlaidifyLinkInstitutionRegistry(
            supportedSites: ["rbc"],
            supportedAuthStyles: ["username_password"]
        )
        let flow = PlaidifyLinkConnectFlow(registry: registry)
        flow.apply(.selectInstitution(makeOrg()))
        flow.apply(.credentialsSubmitted)
        flow.apply(.connectResponded(PlaidifyConnectResponse(
            status: "mfa_required",
            sessionID: "sess-9", mfaType: "otp",
            publicToken: nil, jobID: nil,
            message: nil, errorMessage: nil
        )))
        XCTAssertEqual(flow.state.step, .mfa)
        XCTAssertEqual(flow.state.sessionID, "sess-9")
        XCTAssertEqual(flow.state.mfaType, "otp")
    }

    func testFallbackEmittedForUnsupportedAuthStyle() {
        var events: [PlaidifyLinkFlowEvent] = []
        let registry = PlaidifyLinkInstitutionRegistry(
            supportedSites: ["rbc"],
            supportedAuthStyles: ["username_password"]
        )
        let flow = PlaidifyLinkConnectFlow(registry: registry) { events.append($0) }

        let oauthOrg = makeOrg(authStyle: "oauth_redirect")
        flow.apply(.selectInstitution(oauthOrg))

        // No native step transition; webview fallback emitted instead.
        XCTAssertNotEqual(flow.state.step, .credentials)
        XCTAssertTrue(events.contains(where: {
            if case .fallbackToWebView(let org, let reason) = $0 {
                return org == oauthOrg && reason.hasPrefix("auth_style_unsupported")
            }
            return false
        }))
    }

    func testFallbackForSiteNotInRegistry() {
        var events: [PlaidifyLinkFlowEvent] = []
        let registry = PlaidifyLinkInstitutionRegistry(
            supportedSites: ["rbc"],
            supportedAuthStyles: ["username_password"]
        )
        let flow = PlaidifyLinkConnectFlow(registry: registry) { events.append($0) }
        flow.apply(.selectInstitution(makeOrg(site: "td")))
        XCTAssertTrue(events.contains(.fallbackToWebView(makeOrg(site: "td"), reason: "site_not_in_native_registry")))
    }

    func testErrorResponseTransitionsToErrorStep() {
        let registry = PlaidifyLinkInstitutionRegistry(
            supportedSites: ["rbc"],
            supportedAuthStyles: ["username_password"]
        )
        let flow = PlaidifyLinkConnectFlow(registry: registry)
        flow.apply(.selectInstitution(makeOrg()))
        flow.apply(.credentialsSubmitted)
        flow.apply(.connectResponded(PlaidifyConnectResponse(
            status: "error",
            sessionID: nil, mfaType: nil,
            publicToken: nil, jobID: nil,
            message: nil, errorMessage: "bad credentials"
        )))
        XCTAssertEqual(flow.state.step, .error)
        XCTAssertEqual(flow.state.lastErrorMessage, "bad credentials")
    }

    func testResetReturnsToPicker() {
        let registry = PlaidifyLinkInstitutionRegistry(
            supportedSites: ["rbc"],
            supportedAuthStyles: ["username_password"]
        )
        let flow = PlaidifyLinkConnectFlow(registry: registry)
        flow.apply(.selectInstitution(makeOrg()))
        flow.apply(.reset)
        XCTAssertEqual(flow.state.step, .picker)
        XCTAssertNil(flow.state.organization)
    }

    func testEmptyRegistryFallsBackForUnknownAuthStyle() {
        let registry = PlaidifyLinkInstitutionRegistry()
        let strategy = registry.strategy(for: PlaidifyOrganization(
            organizationID: "1", name: "X", site: "x",
            logoURL: nil, primaryColor: nil, accentColor: nil,
            secondaryColor: nil, hintCopy: nil, authStyle: nil
        ))
        if case .webViewFallback = strategy { } else {
            XCTFail("expected webViewFallback")
        }
    }
}
