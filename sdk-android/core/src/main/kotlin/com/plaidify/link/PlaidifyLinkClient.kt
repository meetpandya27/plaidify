package com.plaidify.link

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/**
 * Minimal HTTP abstraction so [PlaidifyLinkClient] is testable without
 * spinning up a real server. [HttpUrlConnectionClient] is the default.
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
    private val http: PlaidifyLinkHttpClient = HttpUrlConnectionClient(),
    private val json: Json = Json { ignoreUnknownKeys = true },
) {

    public suspend fun getStatus(): PlaidifyLinkSessionStatus {
        val url = PlaidifyLinkUrlBuilder.status(serverUrl, linkToken)
        val raw = send(PlaidifyLinkHttpClient.HttpRequest(method = "GET", url = url))
        return decode(PlaidifyLinkSessionStatus.serializer(), raw)
    }

    public suspend fun searchOrganizations(
        query: String? = null,
        site: String? = null,
        limit: Int = 40,
    ): PlaidifyOrganizationSearchResponse {
        val url = PlaidifyLinkUrlBuilder.organizationSearch(serverUrl, query, site, limit)
        val raw = send(PlaidifyLinkHttpClient.HttpRequest(method = "GET", url = url))
        return decode(PlaidifyOrganizationSearchResponse.serializer(), raw)
    }

    public suspend fun getEncryptionPublicKey(): PlaidifyEncryptionKey {
        val url = PlaidifyLinkUrlBuilder.encryptionPublicKey(serverUrl, linkToken)
        val raw = send(PlaidifyLinkHttpClient.HttpRequest(method = "GET", url = url))
        return decode(PlaidifyEncryptionKey.serializer(), raw)
    }

    public suspend fun connect(
        site: String,
        encrypted: PlaidifyEncryptedCredentials,
    ): PlaidifyConnectResponse {
        val body = buildJsonObject {
            put("link_token", linkToken)
            put("site", site)
            put("encrypted_username", encrypted.username)
            put("encrypted_password", encrypted.password)
        }
        val raw = postJson(PlaidifyLinkUrlBuilder.connect(serverUrl), body)
        return decode(PlaidifyConnectResponse.serializer(), raw)
    }

    /** The code travels in the JSON body, never the URL (URLs land in access logs). */
    public suspend fun submitMfa(sessionId: String, code: String): PlaidifyConnectResponse {
        val body = buildJsonObject {
            put("session_id", sessionId)
            put("code", code)
        }
        val raw = postJson(PlaidifyLinkUrlBuilder.mfaSubmit(serverUrl), body)
        return decode(PlaidifyConnectResponse.serializer(), raw)
    }

    private suspend fun postJson(url: String, body: JsonObject): String =
        send(
            PlaidifyLinkHttpClient.HttpRequest(
                method = "POST",
                url = url,
                headers = mapOf("Content-Type" to "application/json"),
                body = json.encodeToString(JsonObject.serializer(), body),
            )
        )

    private fun <T> decode(deserializer: kotlinx.serialization.DeserializationStrategy<T>, raw: String): T =
        try {
            json.decodeFromString(deserializer, raw)
        } catch (e: Exception) {
            throw PlaidifyLinkClientException.Decoding(e.message ?: "Could not decode the server reply.")
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

    /** `{"detail": str | [{"msg": str}]}` (FastAPI) or `{"error", "error_code"}`. */
    private fun parseError(body: String, status: Int): Pair<String?, String> {
        val obj = try {
            json.parseToJsonElement(body).jsonObject
        } catch (e: Exception) {
            return null to "HTTP $status"
        }
        val detail = when (val element = obj["detail"]) {
            is JsonPrimitive -> element.contentOrNull
            is JsonArray -> element
                .mapNotNull { (it as? JsonObject)?.get("msg") as? JsonPrimitive }
                .mapNotNull { it.contentOrNull }
                .joinToString("; ")
                .ifEmpty { null }
            else -> null
        }
        val error = (obj["error"] as? JsonPrimitive)?.contentOrNull
        val code = (obj["error_code"] as? JsonPrimitive)?.contentOrNull
        return code to (detail ?: error ?: "HTTP $status")
    }
}
