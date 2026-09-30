import Foundation

/// Runs the experimental native screens against the server, the way the
/// hosted page does: encrypt → `/connect` → follow the session until it
/// completes, needs MFA, or fails; MFA answers go to `/mfa/submit` and the
/// session is followed again. Every outcome lands in ``flow``, so the UI
/// never waits on a request nobody makes.
@MainActor
public final class PlaidifyLinkNativeSession {
    public let client: PlaidifyLinkClient
    public let flow: PlaidifyLinkConnectFlow

    private let encryptor: PlaidifyCredentialEncrypting
    private let pollInterval: UInt64
    private let maxPolls: Int
    private let sleep: (UInt64) async throws -> Void

    /// - Parameters:
    ///   - pollInterval: nanoseconds between status polls (default 1.1 s,
    ///     like the hosted page).
    ///   - maxPolls: polls before giving up (default 90, ~100 s).
    ///   - sleep: injectable for tests.
    public init(
        client: PlaidifyLinkClient,
        flow: PlaidifyLinkConnectFlow,
        encryptor: PlaidifyCredentialEncrypting = PlaidifyRSAOAEPEncryptor(),
        pollInterval: UInt64 = 1_100_000_000,
        maxPolls: Int = 90,
        sleep: @escaping (UInt64) async throws -> Void = { try await Task.sleep(nanoseconds: $0) }
    ) {
        self.client = client
        self.flow = flow
        self.encryptor = encryptor
        self.pollInterval = pollInterval
        self.maxPolls = maxPolls
        self.sleep = sleep
    }

    /// Encrypt and submit the credentials for the selected organization.
    public func submitCredentials(username: String, password: String) async {
        guard let organization = flow.state.organization else {
            fail("Choose a provider before continuing.")
            return
        }
        flow.apply(.credentialsSubmitted)
        do {
            let key = try await client.getEncryptionPublicKey()
            let encrypted = PlaidifyEncryptedCredentials(
                username: try encryptor.encrypt(username, publicKeyPEM: key.publicKey),
                password: try encryptor.encrypt(password, publicKeyPEM: key.publicKey)
            )
            let response = try await client.connect(site: organization.site, encrypted: encrypted)
            await handle(response, answeredMFASessionID: nil)
        } catch {
            fail(error)
        }
    }

    /// Answer the current MFA challenge. An empty `code` (push approval)
    /// just waits for the provider to see the approval.
    public func submitMFA(code: String) async {
        guard let sessionID = flow.state.sessionID else {
            fail("There is no verification waiting for an answer.")
            return
        }
        flow.apply(.mfaSubmitted)
        do {
            if code.isEmpty {
                await follow(answeredMFASessionID: sessionID)
                return
            }
            let response = try await client.submitMFA(sessionID: sessionID, code: code)
            await handle(response, answeredMFASessionID: sessionID)
        } catch {
            fail(error)
        }
    }

    // MARK: - Internals

    private func handle(_ response: PlaidifyConnectResponse, answeredMFASessionID: String?) async {
        switch response.status {
        case "connected", "completed":
            await succeed(publicToken: response.publicToken, jobID: response.jobID)
        case "mfa_required":
            flow.apply(.connectResponded(response))
        case "pending", "mfa_submitted":
            await follow(answeredMFASessionID: answeredMFASessionID)
        case "error":
            fail(response.error ?? response.message ?? "The connection could not be completed.")
        default:
            fail("Unexpected status: \(response.status)")
        }
    }

    /// Poll the link session until it settles. The challenge that was just
    /// answered keeps reading `mfa_required` until the job moves on, so only
    /// a different MFA session counts as a new prompt.
    private func follow(answeredMFASessionID: String?) async {
        for attempt in 0..<maxPolls {
            if attempt > 0 {
                do {
                    try await sleep(pollInterval)
                } catch {
                    return
                }
            }
            let status: PlaidifyLinkSessionStatus
            do {
                status = try await client.getStatus()
            } catch {
                fail(error)
                return
            }
            switch status.status {
            case "completed":
                await succeed(publicToken: status.publicToken, jobID: status.jobID)
                return
            case "mfa_required":
                if let answered = answeredMFASessionID, status.sessionID == nil || status.sessionID == answered {
                    continue
                }
                flow.apply(.connectResponded(PlaidifyConnectResponse(
                    status: "mfa_required",
                    sessionID: status.sessionID,
                    mfaType: status.mfaType,
                    jobID: status.jobID,
                    message: status.message
                )))
                return
            case "error":
                fail(status.errorMessage ?? "The connection could not be completed.")
                return
            case "expired", "exited":
                fail("This link has \(status.status). Request a fresh link to continue.")
                return
            default:
                continue
            }
        }
        flow.apply(.failed(
            code: "mfa_timeout",
            message: "The connection timed out before the provider completed the flow."
        ))
    }

    /// `/connect` does not always carry the public token; the session does.
    private func succeed(publicToken: String?, jobID: String?) async {
        var token = publicToken
        var job = jobID
        if token == nil, let status = try? await client.getStatus(), status.status == "completed" {
            token = status.publicToken
            job = job ?? status.jobID
        }
        flow.apply(.connectResponded(PlaidifyConnectResponse(
            status: "completed",
            publicToken: token,
            jobID: job
        )))
    }

    private func fail(_ message: String) {
        flow.apply(.failed(code: nil, message: message))
    }

    private func fail(_ error: Error) {
        switch error {
        case PlaidifyLinkClientError.http(_, let code, let message):
            flow.apply(.failed(code: code, message: message))
        case PlaidifyLinkClientError.transport(let message):
            flow.apply(.failed(code: "network_error", message: message))
        default:
            flow.apply(.failed(code: nil, message: "\(error)"))
        }
    }
}
