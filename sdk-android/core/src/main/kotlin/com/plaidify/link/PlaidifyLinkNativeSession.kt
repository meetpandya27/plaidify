package com.plaidify.link

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay

/**
 * Runs the experimental native screens against the server, the way the
 * hosted page does: encrypt → `/connect` → follow the session until it
 * completes, needs MFA, or fails; MFA answers go to `/mfa/submit` and the
 * session is followed again. Every outcome lands in [flow], so the UI never
 * waits on a request nobody makes.
 *
 * Call from one coroutine at a time (e.g. the Activity's lifecycleScope on
 * the main dispatcher); [flow] events fire on the caller's thread.
 */
public class PlaidifyLinkNativeSession(
    public val client: PlaidifyLinkClient,
    public val flow: PlaidifyLinkConnectFlow,
    private val encryptor: PlaidifyCredentialEncryptor = RsaOaepEncryptor,
    private val pollIntervalMillis: Long = 1_100,
    private val maxPolls: Int = 90,
    private val sleep: suspend (Long) -> Unit = { delay(it) },
) {
    /** Encrypt and submit the credentials for the selected organization. */
    public suspend fun submitCredentials(username: String, password: String) {
        val organization = flow.state.organization
            ?: return fail("Choose a provider before continuing.")
        flow.apply(PlaidifyLinkConnectFlow.Action.CredentialsSubmitted)
        guarded {
            val key = client.getEncryptionPublicKey()
            val encrypted = PlaidifyEncryptedCredentials(
                username = encryptor.encrypt(username, key.publicKey),
                password = encryptor.encrypt(password, key.publicKey),
            )
            handle(client.connect(organization.site, encrypted), answeredMfaSessionId = null)
        }
    }

    /**
     * Answer the current MFA challenge. An empty [code] (push approval) just
     * waits for the provider to see the approval.
     */
    public suspend fun submitMfa(code: String) {
        val sessionId = flow.state.sessionId
            ?: return fail("There is no verification waiting for an answer.")
        flow.apply(PlaidifyLinkConnectFlow.Action.MfaSubmitted)
        guarded {
            if (code.isEmpty()) {
                follow(answeredMfaSessionId = sessionId)
            } else {
                handle(client.submitMfa(sessionId, code), answeredMfaSessionId = sessionId)
            }
        }
    }

    private suspend fun handle(response: PlaidifyConnectResponse, answeredMfaSessionId: String?) {
        when (response.status) {
            "connected", "completed" -> succeed(response.publicToken, response.jobId)
            "mfa_required" -> flow.apply(PlaidifyLinkConnectFlow.Action.ConnectResponded(response))
            "pending", "mfa_submitted" -> follow(answeredMfaSessionId)
            "error" -> fail(response.error ?: response.message ?: "The connection could not be completed.")
            else -> fail("Unexpected status: ${response.status}")
        }
    }

    /**
     * Poll the link session until it settles. The challenge that was just
     * answered keeps reading `mfa_required` until the job moves on, so only a
     * different MFA session counts as a new prompt.
     */
    private suspend fun follow(answeredMfaSessionId: String?) {
        repeat(maxPolls) { attempt ->
            if (attempt > 0) sleep(pollIntervalMillis)
            val status = client.getStatus()
            when (status.status) {
                "completed" -> return succeed(status.publicToken, status.jobId)
                "mfa_required" -> {
                    val sameChallenge = answeredMfaSessionId != null &&
                        (status.sessionId == null || status.sessionId == answeredMfaSessionId)
                    if (!sameChallenge) {
                        flow.apply(
                            PlaidifyLinkConnectFlow.Action.ConnectResponded(
                                PlaidifyConnectResponse(
                                    status = "mfa_required",
                                    sessionId = status.sessionId,
                                    mfaType = status.mfaType,
                                    jobId = status.jobId,
                                    message = status.message,
                                )
                            )
                        )
                        return
                    }
                }
                "error" -> return fail(status.errorMessage ?: "The connection could not be completed.")
                "expired", "exited" -> return fail("This link has ${status.status}. Request a fresh link to continue.")
            }
        }
        flow.apply(
            PlaidifyLinkConnectFlow.Action.Failed(
                code = "mfa_timeout",
                message = "The connection timed out before the provider completed the flow.",
            )
        )
    }

    /** `/connect` does not always carry the public token; the session does. */
    private suspend fun succeed(publicToken: String?, jobId: String?) {
        var token = publicToken
        var job = jobId
        if (token == null) {
            val status = runCatching { client.getStatus() }.getOrNull()
            if (status?.status == "completed") {
                token = status.publicToken
                job = job ?: status.jobId
            }
        }
        flow.apply(
            PlaidifyLinkConnectFlow.Action.ConnectResponded(
                PlaidifyConnectResponse(status = "completed", publicToken = token, jobId = job)
            )
        )
    }

    private inline fun guarded(block: () -> Unit) {
        try {
            block()
        } catch (e: CancellationException) {
            throw e
        } catch (e: PlaidifyLinkClientException.Http) {
            flow.apply(PlaidifyLinkConnectFlow.Action.Failed(e.errorCode, e.message ?: "HTTP ${e.status}"))
        } catch (e: PlaidifyLinkClientException.Transport) {
            flow.apply(PlaidifyLinkConnectFlow.Action.Failed("network_error", e.message ?: "Network error."))
        } catch (e: Exception) {
            flow.apply(PlaidifyLinkConnectFlow.Action.Failed(null, e.message ?: e.toString()))
        }
    }

    private fun fail(message: String) {
        flow.apply(PlaidifyLinkConnectFlow.Action.Failed(code = null, message = message))
    }
}
