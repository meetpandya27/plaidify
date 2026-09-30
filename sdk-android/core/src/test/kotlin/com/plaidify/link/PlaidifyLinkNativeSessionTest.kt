package com.plaidify.link

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull

class PlaidifyLinkNativeSessionTest {
    private val org = PlaidifyOrganization(
        organizationId = "org-1",
        name = "Anchor Point Bank",
        site = "hydro_one",
        authStyle = "username_password",
    )

    private fun session(vararg responses: Pair<Int, String>, maxPolls: Int = 5): Pair<PlaidifyLinkNativeSession, StubHttpClient> {
        val stub = StubHttpClient(responses.map { StubHttpClient.Response(it.first, it.second) })
        val flow = PlaidifyLinkConnectFlow(
            PlaidifyLinkInstitutionRegistry(supportedSites = setOf("hydro_one"))
        )
        flow.apply(PlaidifyLinkConnectFlow.Action.SelectInstitution(org))
        val session = PlaidifyLinkNativeSession(
            client = PlaidifyLinkClient(serverUrl = "https://api.example.com", linkToken = "lnk-1", http = stub),
            flow = flow,
            encryptor = { plaintext, _ -> "enc($plaintext)" },
            maxPolls = maxPolls,
            sleep = {},
        )
        return session to stub
    }

    private fun paths(stub: StubHttpClient) = stub.recordedRequests.map { it.url.removePrefix("https://api.example.com") }

    private fun body(request: PlaidifyLinkHttpClient.HttpRequest): Map<String, String> =
        Json.parseToJsonElement(assertNotNull(request.body)).jsonObject.mapValues { it.value.jsonPrimitive.content }

    @Test
    fun credentialsAreEncryptedSentAndTheTokenComesFromTheSession() = runTest {
        val (session, stub) = session(
            200 to """{"link_token":"lnk-1","public_key":"PEM"}""",
            200 to """{"status":"connected","job_id":"job-1"}""",
            200 to """{"status":"completed","public_token":"public-1","job_id":"job-1"}""",
        )

        session.submitCredentials("alice", "hunter22")

        assertEquals(PlaidifyLinkStep.Success, session.flow.state.step)
        assertEquals("public-1", session.flow.state.publicToken)
        assertEquals(listOf("/encryption/public_key/lnk-1", "/connect", "/link/sessions/lnk-1/status"), paths(stub))
        assertEquals(
            mapOf(
                "link_token" to "lnk-1",
                "site" to "hydro_one",
                "encrypted_username" to "enc(alice)",
                "encrypted_password" to "enc(hunter22)",
            ),
            body(stub.recordedRequests[1]),
        )
    }

    @Test
    fun mfaIsAnsweredAndTheAnsweredChallengeIsNotShownAgain() = runTest {
        val (session, stub) = session(
            200 to """{"link_token":"lnk-1","public_key":"PEM"}""",
            200 to """{"status":"mfa_required","session_id":"mfa-1","mfa_type":"otp","metadata":{"message":"Enter the code"}}""",
            200 to """{"status":"mfa_submitted","message":"Code submitted."}""",
            200 to """{"status":"mfa_required","session_id":"mfa-1"}""",
            200 to """{"status":"completed","public_token":"public-2"}""",
        )

        session.submitCredentials("alice", "hunter22")
        assertEquals(PlaidifyLinkStep.Mfa, session.flow.state.step)
        assertEquals("Enter the code", session.flow.state.mfaPrompt)

        session.submitMfa("123456")

        assertEquals(PlaidifyLinkStep.Success, session.flow.state.step)
        assertEquals("public-2", session.flow.state.publicToken)
        val mfa = stub.recordedRequests[2]
        assertEquals("https://api.example.com/mfa/submit", mfa.url)
        assertEquals(mapOf("session_id" to "mfa-1", "code" to "123456"), body(mfa))
    }

    @Test
    fun mfaErrorReplyEndsOnTheErrorStep() = runTest {
        val (session, _) = session(
            200 to """{"link_token":"lnk-1","public_key":"PEM"}""",
            200 to """{"status":"mfa_required","session_id":"mfa-1","mfa_type":"otp"}""",
            200 to """{"status":"error","error":"MFA session not found or expired."}""",
        )
        session.submitCredentials("alice", "hunter22")
        session.submitMfa("123456")

        assertEquals(PlaidifyLinkStep.Error, session.flow.state.step)
        assertEquals("MFA session not found or expired.", session.flow.state.lastErrorMessage)
    }

    @Test
    fun pushApprovalWaitsOnTheSessionWithoutPostingACode() = runTest {
        val (session, stub) = session(
            200 to """{"link_token":"lnk-1","public_key":"PEM"}""",
            200 to """{"status":"mfa_required","session_id":"mfa-push","mfa_type":"push"}""",
            200 to """{"status":"completed","public_token":"public-3"}""",
        )
        session.submitCredentials("alice", "hunter22")
        session.submitMfa("")

        assertEquals(PlaidifyLinkStep.Success, session.flow.state.step)
        assertFalse(paths(stub).contains("/mfa/submit"))
    }

    @Test
    fun pendingConnectFollowsTheSessionToItsError() = runTest {
        val (session, _) = session(
            200 to """{"link_token":"lnk-1","public_key":"PEM"}""",
            200 to """{"status":"pending","job_id":"job-1"}""",
            200 to """{"status":"connecting"}""",
            200 to """{"status":"error","error_message":"Invalid credentials"}""",
        )
        session.submitCredentials("alice", "wrong")

        assertEquals(PlaidifyLinkStep.Error, session.flow.state.step)
        assertEquals("Invalid credentials", session.flow.state.lastErrorMessage)
    }

    @Test
    fun givesUpInsteadOfConnectingForever() = runTest {
        val (session, _) = session(
            200 to """{"link_token":"lnk-1","public_key":"PEM"}""",
            200 to """{"status":"pending"}""",
            200 to """{"status":"connecting"}""",
            200 to """{"status":"connecting"}""",
            maxPolls = 2,
        )
        session.submitCredentials("alice", "hunter22")

        assertEquals(PlaidifyLinkStep.Error, session.flow.state.step)
        assertEquals("mfa_timeout", session.flow.state.lastErrorCode)
    }

    @Test
    fun httpErrorsLandOnTheErrorStep() = runTest {
        val (session, _) = session(
            200 to """{"link_token":"lnk-1","public_key":"PEM"}""",
            429 to """{"error":"Too many attempts","error_code":"rate_limited"}""",
        )
        session.submitCredentials("alice", "hunter22")

        assertEquals(PlaidifyLinkStep.Error, session.flow.state.step)
        assertEquals("rate_limited", session.flow.state.lastErrorCode)
        assertEquals("Too many attempts", session.flow.state.lastErrorMessage)
    }
}
