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
        let mfa = PlaidifyLinkMessageParser.parse(string: "{\"source\":\"plaidify-link\",\"event\":\"MFA_REQUIRED\"}")

        XCTAssertEqual(connected?.isTerminal, true)
        XCTAssertEqual(connected?.shouldDismissSheet, true)
        XCTAssertEqual(mfa?.isTerminal, false)
        XCTAssertEqual(mfa?.shouldDismissSheet, false)
    }
}