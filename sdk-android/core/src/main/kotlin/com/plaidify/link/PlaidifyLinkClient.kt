package com.plaidify.link

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * Minimal HTTP abstraction so [PlaidifyLinkClient] is testable without
 * spinning up a real server.
 */
public interface PlaidifyLinkHttpClient {
    public suspend fun execute(request: HttpRequest): HttpResponse

    public data class HttpRequest(
        val method: String,
        val url: String,
        val headers: Map<String, String> = emptyMap(),
        val body: String? = null,
    )

    public data class HttpResponse(
        val status: Int,
        val body: String,
    )
}

/**
 * REST client that talks to the same endpoints as the React frontend.
 *
 * All methods are `suspend` so the host app can call them from a
 * coroutine scope (e.g. `viewModelScope.launch { ... }`).
 */
public class PlaidifyLinkClient(
    public val serverUrl: String,
    public val linkToken: String,
    private val http: PlaidifyLinkHttpClient,
    private val json: Json = Json { ignoreUnknownKeys = true },
) {

    public suspend fun getStatus(): PlaidifyLinkSessionStatus {
        val url = PlaidifyLinkUrlBuilder.status(serverUrl, linkToken)
        val raw = send(PlaidifyLinkHttpClient.HttpRequest(method = "GET", url = url))
        return json.decodeFromString(PlaidifyLinkSessionStatus.serializer(), raw)
    }

    public suspend fun searchOrganizations(
        query: String? = null,
        site: String? = null,
        limit: Int = 40,
    ): PlaidifyOrganizationSearchResponse {
        val url = PlaidifyLinkUrlBuilder.organizationSearch(serverUrl, query, site, limit)
        val raw = send(PlaidifyLinkHttpClient.HttpRequest(method = "GET", url = url))
        return json.decodeFromString(PlaidifyOrganizationSearchResponse.serializer(), raw)
    }

    public suspend fun connect(
        site: String,
        encrypted: PlaidifyEncryptedCredentials,
    ): PlaidifyConnectResponse {
        val url = PlaidifyLinkUrlBuilder.connect(serverUrl)
        val body = buildString {
            append('{')
            append("\"link_token\":").append(jsonEncode(linkToken)).append(',')
            append("\"site\":").append(jsonEncode(site)).append(',')
            append("\"encrypted_username\":").append(jsonEncode(encrypted.username)).append(',')
            append("\"encrypted_password\":").append(jsonEncode(encrypted.password))
            append('}')
        }
        val raw = send(
            PlaidifyLinkHttpClient.HttpRequest(
                method = "POST",
                url = url,
                headers = mapOf("Content-Type" to "application/json"),
                body = body,
            )
        )
        return json.decodeFromString(PlaidifyConnectResponse.serializer(), raw)
    }

    public suspend fun submitMfa(sessionId: String, code: String): PlaidifyConnectResponse {
        val url = PlaidifyLinkUrlBuilder.mfaSubmit(serverUrl, sessionId, code)
        val raw = send(PlaidifyLinkHttpClient.HttpRequest(method = "POST", url = url))
        return json.decodeFromString(PlaidifyConnectResponse.serializer(), raw)
    }

    private suspend fun send(request: PlaidifyLinkHttpClient.HttpRequest): String {
        val response = try {
            http.execute(
                request.copy(headers = request.headers + ("Accept" to "application/json"))
            )
        } catch (t: Throwable) {
            throw PlaidifyLinkClientException.Transport(t.message ?: t::class.simpleName ?: "transport error")
        }
        if (response.status !in 200..299) {
            val (errorCode, message) = parseError(response.body, response.status)
            throw PlaidifyLinkClientException.Http(response.status, errorCode, message)
        }
        return response.body
    }

    private fun parseError(body: String, status: Int): Pair<String?, String> {
        return try {
            val obj: JsonObject = json.parseToJsonElement(body).jsonObject
            val detail = obj["detail"]?.jsonPrimitive?.contentOrNull
            val error = obj["error"]?.jsonPrimitive?.contentOrNull
            val code = obj["error_code"]?.jsonPrimitive?.contentOrNull
            code to (detail ?: error ?: "HTTP $status")
        } catch (e: Exception) {
            null to "HTTP $status"
        }
    }

    private fun jsonEncode(value: String): String {
        val sb = StringBuilder("\"")
        for (ch in value) {
            when (ch) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> if (ch.code < 0x20) {
                    sb.append("\\u").append(String.format("%04x", ch.code))
                } else {
                    sb.append(ch)
                }
            }
        }
        sb.append('"')
        return sb.toString()
    }
}
