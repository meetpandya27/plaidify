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
    fun mfaSubmitUrl() {
        val url = PlaidifyLinkUrlBuilder.mfaSubmit("https://api.example.com", "sess-1", "123456")
        assertEquals("https://api.example.com/mfa/submit?session_id=sess-1&code=123456", url)
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
