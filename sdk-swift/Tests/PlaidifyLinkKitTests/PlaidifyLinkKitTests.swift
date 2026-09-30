import Foundation
import XCTest
@testable import PlaidifyLinkKit

final class PlaidifyLinkKitTests: XCTestCase {
    func testHostedLinkURLIncludesOriginAndTheme() throws {
        let configuration = PlaidifyHostedLinkConfiguration(
            serverURL: URL(string: "https://api.example.com/")!,
            token: "lnk-123",
            origin: "myapp://callback",
            theme: PlaidifyLinkTheme(
                accentColor: "#0b8f73",
                backgroundColor: "#f4f7fb",
                borderRadius: "28px"
            )
        )

        let url = try XCTUnwrap(configuration.hostedLinkURL().absoluteString)
        XCTAssertTrue(url.contains("https://api.example.com/link?token=lnk-123"))
        XCTAssertTrue(url.contains("origin=myapp://callback") || url.contains("origin=myapp%3A%2F%2Fcallback"))
        XCTAssertTrue(url.contains("accent=%230b8f73"))
        XCTAssertTrue(url.contains("bg=%23f4f7fb"))
        XCTAssertTrue(url.contains("radius=28px"))
    }

    func testMessageParserParsesJSONString() {
        let payload = PlaidifyLinkMessageParser.parse(string: "{\"source\":\"plaidify-link\",\"event\":\"CONNECTED\",\"public_token\":\"public-123\",\"job_id\":\"job-1\"}")

        XCTAssertEqual(payload?.name, .connected)
        XCTAssertEqual(payload?.publicToken, "public-123")
        XCTAssertEqual(payload?.jobID, "job-1")
    }

    func testMessageParserParsesDictionaryBody() {
        let body: [String: Any] = [
            "source": "plaidify-link",
            "event": "MFA_REQUIRED",
            "mfa_type": "otp",
            "session_id": "sess-123",
        ]

        let payload = PlaidifyLinkMessageParser.parse(body: body)

        XCTAssertEqual(payload?.name, .mfaRequired)
        XCTAssertEqual(payload?.mfaType, "otp")
        XCTAssertEqual(payload?.sessionID, "sess-123")
    }

    func testMessageParserRejectsNonPlaidifyPayloads() {
        let body: [String: Any] = [
            "source": "other-source",
            "event": "CONNECTED",
        ]

        XCTAssertNil(PlaidifyLinkMessageParser.parse(body: body))
    }

    func testTerminalEventsDismissTheSheet() {
        let connected = PlaidifyLinkMessageParser.parse(string: "{\"source\":\"plaidify-link\",\"event\":\"CONNECTED\"}")
        let exit = PlaidifyLinkMessageParser.parse(string: "{\"source\":\"plaidify-link\",\"event\":\"EXIT\",\"reason\":\"user_exit\",\"error_code\":\"rate_limited\"}")
        let mfa = PlaidifyLinkMessageParser.parse(string: "{\"source\":\"plaidify-link\",\"event\":\"MFA_REQUIRED\"}")

        XCTAssertEqual(connected?.isTerminal, true)
        XCTAssertEqual(connected?.shouldDismissSheet, true)
        XCTAssertEqual(exit?.shouldDismissSheet, true)
        XCTAssertEqual(exit?.errorCode, "rate_limited")
        XCTAssertEqual(mfa?.isTerminal, false)
        XCTAssertEqual(mfa?.shouldDismissSheet, false)
    }

    func testErrorDoesNotDismissTheSheet() {
        // The page shows retry / choose-another-provider after an ERROR.
        let error = PlaidifyLinkMessageParser.parse(string: "{\"source\":\"plaidify-link\",\"event\":\"ERROR\",\"error\":\"bad password\"}")
        XCTAssertEqual(error?.name, .error)
        XCTAssertEqual(error?.isTerminal, false)
        XCTAssertEqual(error?.shouldDismissSheet, false)
    }

    func testLogoDataURIKeepsItsPlusSigns() throws {
        let logo = "data:image/png;base64,iVBORw0KGgo+AAA/BBB="
        let configuration = PlaidifyHostedLinkConfiguration(
            serverURL: URL(string: "https://api.example.com")!,
            token: "lnk-1",
            theme: PlaidifyLinkTheme(logo: logo)
        )
        let components = try XCTUnwrap(URLComponents(url: configuration.hostedLinkURL(), resolvingAgainstBaseURL: false))
        XCTAssertFalse(components.percentEncodedQuery?.contains("+") ?? true)
        XCTAssertEqual(components.queryItems?.first(where: { $0.name == "logo" })?.value, logo)
    }

    // MARK: Origin checks (LNK-11)

    func testBridgeAcceptsOnlyThePlaidifyMainFrame() throws {
        let origin = try XCTUnwrap(PlaidifyLinkOrigin(url: URL(string: "https://api.example.com/link?token=x")!))

        XCTAssertTrue(origin.acceptsMessage(isMainFrame: true, scheme: "https", host: "api.example.com", port: 0))
        XCTAssertTrue(origin.acceptsMessage(isMainFrame: true, scheme: "https", host: "API.example.com", port: 443))
        // A frame inside the page, even on the same origin.
        XCTAssertFalse(origin.acceptsMessage(isMainFrame: false, scheme: "https", host: "api.example.com", port: 0))
        // The web view navigated elsewhere.
        XCTAssertFalse(origin.acceptsMessage(isMainFrame: true, scheme: "https", host: "evil.example", port: 0))
        XCTAssertFalse(origin.acceptsMessage(isMainFrame: true, scheme: "http", host: "api.example.com", port: 0))
        XCTAssertFalse(origin.acceptsMessage(isMainFrame: true, scheme: "https", host: "api.example.com", port: 8443))
    }

    func testNavigationStaysOnThePlaidifyOrigin() throws {
        let origin = try XCTUnwrap(PlaidifyLinkOrigin(url: URL(string: "http://localhost:8000")!))
        let decide = { (url: String, mainFrame: Bool) in
            PlaidifyLinkNavigationPolicy.decide(url: URL(string: url), isMainFrame: mainFrame, allowedOrigin: origin)
        }

        XCTAssertEqual(decide("http://localhost:8000/link?token=abc", true), .allow)
        XCTAssertEqual(decide("http://localhost:8000/ui-next/assets/app.js", false), .allow)
        XCTAssertEqual(
            decide("https://bank.example/help", true),
            .openExternally(URL(string: "https://bank.example/help")!)
        )
        XCTAssertEqual(decide("https://tracker.example/frame", false), .cancel)
        XCTAssertEqual(decide("http://localhost:9999/", false), .cancel)
        XCTAssertEqual(decide("javascript:alert(1)", true), .cancel)
        XCTAssertEqual(decide("file:///etc/passwd", true), .cancel)
    }
}