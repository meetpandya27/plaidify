package com.plaidify.link

import kotlinx.coroutines.test.runTest
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class PlaidifyLinkClientTest {
    @Test
    fun statusUrlEscapesToken() {
        val url = PlaidifyLinkUrlBuilder.status("https://api.example.com/", "tok with space")
        assertEquals("https://api.example.com/link/sessions/tok%20with%20space/status", url)
    }

    @Test
    fun organizationSearchEncodesQuery() {
        val url = PlaidifyLinkUrlBuilder.organizationSearch(
            serverUrl = "https://api.example.com",
            query = "Royal Bank",
            site = "rbc",
            limit = 25,
        )
        assertTrue(url.startsWith("https://api.example.com/organizations/search?"))
        assertTrue(url.contains("limit=25"))
        assertTrue(url.contains("q=Royal%20Bank"))
        assertTrue(url.contains("site=rbc"))
    }

    @Test
    fun submitMfaPostsCodeInBodyNotUrl() = runTest {
        val stub = StubHttpClient(listOf(StubHttpClient.Response(200, """{"status":"mfa_submitted"}""")))
        val client = PlaidifyLinkClient(
            serverUrl = "https://api.example.com",
            linkToken = "tok",
            http = stub,
        )
        val response = client.submitMfa(sessionId = "sess-1", code = "123456")
        assertEquals("mfa_submitted", response.status)

        val recorded = stub.recordedRequests.first()
        assertEquals("POST", recorded.method)
        assertEquals("https://api.example.com/mfa/submit", recorded.url)
        assertEquals("""{"session_id":"sess-1","code":"123456"}""", recorded.body)
    }

    @Test
    fun getStatusDecodesPayload() = runTest {
        val stub = StubHttpClient(
            listOf(
                StubHttpClient.Response(
                    200,
                    """{"status":"awaiting_credentials","site":"rbc"}""",
                )
            )
        )
        val client = PlaidifyLinkClient(
            serverUrl = "https://api.example.com",
            linkToken = "tok",
            http = stub,
        )
        val status = client.getStatus()
        assertEquals("awaiting_credentials", status.status)
        assertEquals("rbc", status.site)
    }

    @Test
    fun httpErrorIsTypedWithErrorCode() = runTest {
        val stub = StubHttpClient(
            listOf(
                StubHttpClient.Response(
                    429,
                    """{"detail":"slow down","error_code":"rate_limited"}""",
                )
            )
        )
        val client = PlaidifyLinkClient(
            serverUrl = "https://api.example.com",
            linkToken = "tok",
            http = stub,
        )
        val error = assertFailsWith<PlaidifyLinkClientException.Http> { client.getStatus() }
        assertEquals(429, error.status)
        assertEquals("rate_limited", error.errorCode)
        assertEquals("slow down", error.message)
    }

    @Test
    fun connectPostsExpectedBody() = runTest {
        val stub = StubHttpClient(
            listOf(
                StubHttpClient.Response(
                    200,
                    """{"status":"completed","public_token":"public-1","job_id":"job-1"}""",
                )
            )
        )
        val client = PlaidifyLinkClient(
            serverUrl = "https://api.example.com",
            linkToken = "tok-abc",
            http = stub,
        )
        val response = client.connect(
            site = "rbc",
            encrypted = PlaidifyEncryptedCredentials(username = "u-enc", password = "p-enc"),
        )
        assertEquals("completed", response.status)
        assertEquals("public-1", response.publicToken)

        val recorded = stub.recordedRequests.first()
        assertEquals("POST", recorded.method)
        assertTrue(recorded.url.endsWith("/connect"))
        val body = assertNotNull(recorded.body)
        assertTrue(body.contains("\"link_token\":\"tok-abc\""))
        assertTrue(body.contains("\"site\":\"rbc\""))
        assertTrue(body.contains("\"encrypted_username\":\"u-enc\""))
        assertTrue(body.contains("\"encrypted_password\":\"p-enc\""))
    }
}

class PlaidifyLinkClientContractTest {
    private fun client(vararg responses: StubHttpClient.Response) = StubHttpClient(responses.toList()).let {
        it to PlaidifyLinkClient(serverUrl = "https://api.example.com/", linkToken = "lnk-1", http = it)
    }

    @Test
    fun searchDecodesResults() = runTest {
        val (_, client) = client(
            StubHttpClient.Response(
                200,
                """{"results":[{"organization_id":"org-1","name":"Anchor Point Bank","site":"hydro_one","auth_style":"username_password","logo_url":"data:x"}],"count":1}""",
            )
        )
        val response = client.searchOrganizations(site = "hydro_one", limit = 1)
        assertEquals(listOf("hydro_one"), response.results.map { it.site })
        assertEquals(1, response.count)
    }

    @Test
    fun getsTheSessionEncryptionKey() = runTest {
        val (stub, client) = client(StubHttpClient.Response(200, """{"link_token":"lnk-1","public_key":"PEM"}"""))
        assertEquals("PEM", client.getEncryptionPublicKey().publicKey)
        assertEquals("https://api.example.com/encryption/public_key/lnk-1", stub.recordedRequests.single().url)
    }

    @Test
    fun mfaErrorReplyCarriesTheReason() = runTest {
        val (_, client) = client(
            StubHttpClient.Response(200, """{"status":"error","error":"MFA session not found or expired."}""")
        )
        val response = client.submitMfa("gone", "123456")
        assertEquals("error", response.status)
        assertEquals("MFA session not found or expired.", response.error)
    }

    @Test
    fun validationErrorsReadAsText() = runTest {
        val (_, client) = client(
            StubHttpClient.Response(422, """{"detail":[{"loc":["body","code"],"msg":"Field required"}]}""")
        )
        val error = assertFailsWith<PlaidifyLinkClientException.Http> { client.submitMfa("s", "") }
        assertEquals(422, error.status)
        assertEquals("Field required", error.message)
    }

    @Test
    fun plaidifyErrorBodyKeepsItsCode() = runTest {
        val (_, client) = client(
            StubHttpClient.Response(429, """{"error":"Too many attempts","error_code":"rate_limited"}""")
        )
        val error = assertFailsWith<PlaidifyLinkClientException.Http> { client.getStatus() }
        assertEquals("rate_limited", error.errorCode)
        assertEquals("Too many attempts", error.message)
    }

    @Test
    fun hostedLinkUrlCarriesTheThemeTheOnlyWayThePageReadsIt() {
        val url = PlaidifyLinkUrlBuilder.hostedLink(
            serverUrl = "https://api.example.com/",
            linkToken = "lnk 1",
            theme = PlaidifyLinkTheme(accentColor = "#0b8f73", logo = "data:image/png;base64,ab+c/d="),
        )
        assertEquals(
            "https://api.example.com/link?token=lnk%201&accent=%230b8f73&logo=data%3Aimage%2Fpng%3Bbase64%2Cab%2Bc%2Fd%3D",
            url,
        )
    }
}

class StubHttpClient(responses: List<Response>) : PlaidifyLinkHttpClient {
    public data class Response(val status: Int, val body: String)

    private val queue = ArrayDeque(responses)
    public val recordedRequests: MutableList<PlaidifyLinkHttpClient.HttpRequest> = mutableListOf()

    override suspend fun execute(request: PlaidifyLinkHttpClient.HttpRequest): PlaidifyLinkHttpClient.HttpResponse {
        recordedRequests += request
        val next = queue.removeFirstOrNull() ?: error("No stub responses left")
        return PlaidifyLinkHttpClient.HttpResponse(next.status, next.body)
    }
}
