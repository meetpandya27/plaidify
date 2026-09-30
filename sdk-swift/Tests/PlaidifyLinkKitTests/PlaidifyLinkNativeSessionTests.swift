import Foundation
import Security
import XCTest
@testable import PlaidifyLinkKit

final class PlaidifyCredentialEncryptorTests: XCTestCase {
    /// A PEM SubjectPublicKeyInfo for `key`, as the server sends it.
    private func pem(for publicKey: SecKey) throws -> String {
        var error: Unmanaged<CFError>?
        let pkcs1 = try XCTUnwrap(SecKeyCopyExternalRepresentation(publicKey, &error) as Data?)
        XCTAssertEqual(pkcs1.count, 270, "2048-bit RSAPublicKey")
        // SEQUENCE { SEQUENCE { rsaEncryption, NULL }, BIT STRING { 0x00, RSAPublicKey } }
        let header: [UInt8] = [
            0x30, 0x82, 0x01, 0x22, 0x30, 0x0D, 0x06, 0x09, 0x2A, 0x86, 0x48, 0x86,
            0xF7, 0x0D, 0x01, 0x01, 0x01, 0x05, 0x00, 0x03, 0x82, 0x01, 0x0F, 0x00,
        ]
        let spki = Data(header) + pkcs1
        return "-----BEGIN PUBLIC KEY-----\n"
            + spki.base64EncodedString(options: [.lineLength64Characters, .endLineWithLineFeed])
            + "\n-----END PUBLIC KEY-----\n"
    }

    func testRoundTripsWithOAEPSHA256() throws {
        var error: Unmanaged<CFError>?
        let privateKey = try XCTUnwrap(SecKeyCreateRandomKey([
            kSecAttrKeyType: kSecAttrKeyTypeRSA,
            kSecAttrKeySizeInBits: 2048,
        ] as CFDictionary, &error))
        let publicKey = try XCTUnwrap(SecKeyCopyPublicKey(privateKey))

        let ciphertext = try PlaidifyRSAOAEPEncryptor().encrypt("hunter22 ✓", publicKeyPEM: try pem(for: publicKey))

        let decrypted = try XCTUnwrap(SecKeyCreateDecryptedData(
            privateKey,
            .rsaEncryptionOAEPSHA256,
            try XCTUnwrap(Data(base64Encoded: ciphertext)) as CFData,
            &error
        ) as Data?)
        XCTAssertEqual(String(data: decrypted, encoding: .utf8), "hunter22 ✓")
    }

    func testRejectsGarbage() {
        XCTAssertThrowsError(try PlaidifyRSAOAEPEncryptor().encrypt("x", publicKeyPEM: "not a key"))
        XCTAssertThrowsError(try PlaidifyRSAOAEPEncryptor().encrypt(
            "x",
            publicKeyPEM: "-----BEGIN PUBLIC KEY-----\nMAUGAytlcA==\n-----END PUBLIC KEY-----"
        ))
    }
}

/// Stands in for RSA so request bodies are predictable.
private struct TagEncryptor: PlaidifyCredentialEncrypting {
    func encrypt(_ plaintext: String, publicKeyPEM: String) throws -> String {
        "enc(\(plaintext))"
    }
}

@MainActor
final class PlaidifyLinkNativeSessionTests: XCTestCase {
    private let org = PlaidifyOrganization(
        organizationID: "org-1", name: "Anchor Point Bank", site: "hydro_one",
        logoURL: nil, primaryColor: nil, accentColor: nil,
        secondaryColor: nil, hintCopy: nil, authStyle: "username_password"
    )

    private func makeSession(_ responses: [StubHTTPClient.Stub], maxPolls: Int = 5)
        -> (PlaidifyLinkNativeSession, StubHTTPClient, [PlaidifyLinkFlowEvent]) {
        let stub = StubHTTPClient(responses: responses)
        let client = PlaidifyLinkClient(
            serverURL: URL(string: "https://api.example.com")!,
            linkToken: "lnk-1",
            http: stub
        )
        let flow = PlaidifyLinkConnectFlow(registry: PlaidifyLinkInstitutionRegistry(
            supportedSites: ["hydro_one"],
            supportedAuthStyles: ["username_password"]
        ))
        flow.apply(.selectInstitution(org))
        let session = PlaidifyLinkNativeSession(
            client: client,
            flow: flow,
            encryptor: TagEncryptor(),
            maxPolls: maxPolls,
            sleep: { _ in }
        )
        return (session, stub, [])
    }

    private func paths(_ stub: StubHTTPClient) -> [String] {
        stub.recordedRequests.map { $0.url?.path ?? "" }
    }

    func testCredentialsAreEncryptedSentAndTheTokenComesFromTheSession() async throws {
        let (session, stub, _) = makeSession([
            .ok(json: #"{"link_token":"lnk-1","public_key":"pem"}"#),
            .ok(json: #"{"status":"connected","job_id":"job-1"}"#),
            .ok(json: #"{"status":"completed","public_token":"public-1","job_id":"job-1"}"#),
        ])

        await session.submitCredentials(username: "alice", password: "hunter22")

        XCTAssertEqual(session.flow.state.step, .success)
        XCTAssertEqual(session.flow.state.publicToken, "public-1")
        XCTAssertEqual(paths(stub), ["/encryption/public_key/lnk-1", "/connect", "/link/sessions/lnk-1/status"])
        let body = try XCTUnwrap(stub.recordedRequests[1].httpBody)
        let sent = try JSONSerialization.jsonObject(with: body) as? [String: String]
        XCTAssertEqual(sent, [
            "link_token": "lnk-1",
            "site": "hydro_one",
            "encrypted_username": "enc(alice)",
            "encrypted_password": "enc(hunter22)",
        ])
    }

    func testMFAIsAnsweredAndTheAnsweredChallengeIsNotShownAgain() async throws {
        let (session, stub, _) = makeSession([
            .ok(json: #"{"link_token":"lnk-1","public_key":"pem"}"#),
            .ok(json: #"{"status":"mfa_required","session_id":"mfa-1","mfa_type":"otp","metadata":{"message":"Enter the code"}}"#),
            .ok(json: #"{"status":"mfa_submitted","message":"Code submitted."}"#),
            // The job has not moved on yet: still the answered challenge.
            .ok(json: #"{"status":"mfa_required","session_id":"mfa-1"}"#),
            .ok(json: #"{"status":"completed","public_token":"public-2"}"#),
        ])

        await session.submitCredentials(username: "alice", password: "hunter22")
        XCTAssertEqual(session.flow.state.step, .mfa)
        XCTAssertEqual(session.flow.state.mfaPrompt, "Enter the code")

        await session.submitMFA(code: "123456")

        XCTAssertEqual(session.flow.state.step, .success)
        XCTAssertEqual(session.flow.state.publicToken, "public-2")
        let mfaBody = try XCTUnwrap(stub.recordedRequests[2].httpBody)
        XCTAssertEqual(
            try JSONSerialization.jsonObject(with: mfaBody) as? [String: String],
            ["session_id": "mfa-1", "code": "123456"]
        )
        XCTAssertEqual(stub.recordedRequests[2].url?.query, nil)
    }

    func testMFAErrorReplyEndsOnTheErrorStep() async {
        let (session, _, _) = makeSession([
            .ok(json: #"{"link_token":"lnk-1","public_key":"pem"}"#),
            .ok(json: #"{"status":"mfa_required","session_id":"mfa-1","mfa_type":"otp"}"#),
            .ok(json: #"{"status":"error","error":"MFA session not found or expired."}"#),
        ])
        await session.submitCredentials(username: "alice", password: "hunter22")
        await session.submitMFA(code: "123456")

        XCTAssertEqual(session.flow.state.step, .error)
        XCTAssertEqual(session.flow.state.lastErrorMessage, "MFA session not found or expired.")
    }

    func testPushApprovalWaitsOnTheSessionWithoutPostingACode() async {
        let (session, stub, _) = makeSession([
            .ok(json: #"{"link_token":"lnk-1","public_key":"pem"}"#),
            .ok(json: #"{"status":"mfa_required","session_id":"mfa-push","mfa_type":"push"}"#),
            .ok(json: #"{"status":"completed","public_token":"public-3"}"#),
        ])
        await session.submitCredentials(username: "alice", password: "hunter22")
        await session.submitMFA(code: "")

        XCTAssertEqual(session.flow.state.step, .success)
        XCTAssertFalse(paths(stub).contains("/mfa/submit"))
    }

    func testPendingConnectFollowsTheSessionToItsError() async {
        let (session, _, _) = makeSession([
            .ok(json: #"{"link_token":"lnk-1","public_key":"pem"}"#),
            .ok(json: #"{"status":"pending","job_id":"job-1"}"#),
            .ok(json: #"{"status":"connecting"}"#),
            .ok(json: #"{"status":"error","error_message":"Invalid credentials"}"#),
        ])
        await session.submitCredentials(username: "alice", password: "wrong")

        XCTAssertEqual(session.flow.state.step, .error)
        XCTAssertEqual(session.flow.state.lastErrorMessage, "Invalid credentials")
    }

    func testGivesUpInsteadOfConnectingForever() async {
        let (session, _, _) = makeSession([
            .ok(json: #"{"link_token":"lnk-1","public_key":"pem"}"#),
            .ok(json: #"{"status":"pending"}"#),
            .ok(json: #"{"status":"connecting"}"#),
            .ok(json: #"{"status":"connecting"}"#),
        ], maxPolls: 2)
        await session.submitCredentials(username: "alice", password: "hunter22")

        XCTAssertEqual(session.flow.state.step, .error)
        XCTAssertEqual(session.flow.state.lastErrorCode, "mfa_timeout")
    }

    func testHTTPErrorsLandOnTheErrorStep() async {
        let (session, _, _) = makeSession([
            .ok(json: #"{"link_token":"lnk-1","public_key":"pem"}"#),
            .status(429, json: #"{"error":"Too many attempts","error_code":"rate_limited"}"#),
        ])
        await session.submitCredentials(username: "alice", password: "hunter22")

        XCTAssertEqual(session.flow.state.step, .error)
        XCTAssertEqual(session.flow.state.lastErrorCode, "rate_limited")
        XCTAssertEqual(session.flow.state.lastErrorMessage, "Too many attempts")
    }
}
