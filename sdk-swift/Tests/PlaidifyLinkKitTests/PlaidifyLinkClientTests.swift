import Foundation
import XCTest
@testable import PlaidifyLinkKit

final class PlaidifyLinkClientTests: XCTestCase {
    func testStatusURLEscapesToken() {
        let url = PlaidifyLinkURLBuilder.status(
            serverURL: URL(string: "https://api.example.com/")!,
            linkToken: "tok/with space"
        )
        XCTAssertEqual(
            url?.absoluteString,
            "https://api.example.com/link/sessions/tok/with%20space/status"
        )
    }

    func testOrganizationSearchEncodesQuery() {
        let url = PlaidifyLinkURLBuilder.organizationSearch(
            serverURL: URL(string: "https://api.example.com")!,
            query: "Royal Bank",
            site: "rbc",
            limit: 25
        )
        let absolute = url?.absoluteString ?? ""
        XCTAssertTrue(absolute.hasPrefix("https://api.example.com/organizations/search?"))
        XCTAssertTrue(absolute.contains("limit=25"))
        XCTAssertTrue(absolute.contains("q=Royal%20Bank"))
        XCTAssertTrue(absolute.contains("site=rbc"))
    }

    func testMFASubmitURL() {
        let url = PlaidifyLinkURLBuilder.mfaSubmit(
            serverURL: URL(string: "https://api.example.com")!,
            sessionID: "sess-1",
            code: "123456"
        )
        XCTAssertEqual(
            url?.absoluteString,
            "https://api.example.com/mfa/submit?session_id=sess-1&code=123456"
        )
    }

    func testGetStatusDecodesPayload() async throws {
        let stub = StubHTTPClient(responses: [
            .ok(json: """
                {"status":"awaiting_credentials","site":"rbc","mfa_type":null,"session_id":null,"public_token":null,"error_message":null}
                """)
        ])
        let client = PlaidifyLinkClient(
            serverURL: URL(string: "https://api.example.com")!,
            linkToken: "tok",
            http: stub
        )
        let status = try await client.getStatus()
        XCTAssertEqual(status.status, "awaiting_credentials")
        XCTAssertEqual(status.site, "rbc")
    }

    func testHTTPErrorIsTypedWithErrorCode() async {
        let stub = StubHTTPClient(responses: [
            .status(429, json: #"{"detail":"slow down","error_code":"rate_limited"}"#)
        ])
        let client = PlaidifyLinkClient(
            serverURL: URL(string: "https://api.example.com")!,
            linkToken: "tok",
            http: stub
        )
        do {
            _ = try await client.getStatus()
            XCTFail("expected error")
        } catch let PlaidifyLinkClientError.http(status, errorCode, message) {
            XCTAssertEqual(status, 429)
            XCTAssertEqual(errorCode, "rate_limited")
            XCTAssertEqual(message, "slow down")
        } catch {
            XCTFail("unexpected error: \(error)")
        }
    }

    func testConnectPostsExpectedBody() async throws {
        let stub = StubHTTPClient(responses: [
            .ok(json: #"{"status":"completed","public_token":"public-1","job_id":"job-1"}"#)
        ])
        let client = PlaidifyLinkClient(
            serverURL: URL(string: "https://api.example.com")!,
            linkToken: "tok-abc",
            http: stub
        )
        let response = try await client.connect(
            site: "rbc",
            encrypted: PlaidifyEncryptedCredentials(username: "u-enc", password: "p-enc")
        )
        XCTAssertEqual(response.status, "completed")
        XCTAssertEqual(response.publicToken, "public-1")

        let recorded = try XCTUnwrap(stub.recordedRequests.first)
        XCTAssertEqual(recorded.httpMethod, "POST")
        XCTAssertEqual(recorded.url?.path, "/connect")
        let body = try XCTUnwrap(recorded.httpBody)
        let parsed = try JSONSerialization.jsonObject(with: body) as? [String: String]
        XCTAssertEqual(parsed?["link_token"], "tok-abc")
        XCTAssertEqual(parsed?["site"], "rbc")
        XCTAssertEqual(parsed?["encrypted_username"], "u-enc")
        XCTAssertEqual(parsed?["encrypted_password"], "p-enc")
    }
}

// MARK: - Test doubles

final class StubHTTPClient: PlaidifyLinkHTTPClient {
    enum Stub {
        case ok(json: String)
        case status(Int, json: String)
    }

    private var queue: [Stub]
    private(set) var recordedRequests: [URLRequest] = []

    init(responses: [Stub]) {
        self.queue = responses
    }

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        recordedRequests.append(request)
        guard !queue.isEmpty else {
            throw URLError(.badServerResponse)
        }
        let next = queue.removeFirst()
        let url = request.url ?? URL(string: "about:blank")!
        switch next {
        case .ok(let json):
            let data = json.data(using: .utf8) ?? Data()
            let response = HTTPURLResponse(
                url: url, statusCode: 200, httpVersion: nil, headerFields: nil
            )!
            return (data, response)
        case .status(let code, let json):
            let data = json.data(using: .utf8) ?? Data()
            let response = HTTPURLResponse(
                url: url, statusCode: code, httpVersion: nil, headerFields: nil
            )!
            return (data, response)
        }
    }
}
