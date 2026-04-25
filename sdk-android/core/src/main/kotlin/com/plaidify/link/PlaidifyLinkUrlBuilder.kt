package com.plaidify.link

import java.net.URI
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

/** Pure URL builders for the Plaidify Link REST endpoints. */
public object PlaidifyLinkUrlBuilder {
    public fun base(serverUrl: String): String =
        if (serverUrl.endsWith("/")) serverUrl.dropLast(1) else serverUrl

    public fun status(serverUrl: String, linkToken: String): String =
        base(serverUrl) + "/link/sessions/" + encodePath(linkToken) + "/status"

    public fun organizationSearch(
        serverUrl: String,
        query: String?,
        site: String?,
        limit: Int,
    ): String {
        val params = mutableListOf("limit=$limit")
        if (!query.isNullOrEmpty()) {
            params += "q=" + encodeQuery(query)
        }
        if (!site.isNullOrEmpty()) {
            params += "site=" + encodeQuery(site)
        }
        return base(serverUrl) + "/organizations/search?" + params.joinToString("&")
    }

    public fun encryptionPublicKey(serverUrl: String, linkToken: String): String =
        base(serverUrl) + "/encryption/public_key/" + encodePath(linkToken)

    public fun connect(serverUrl: String): String =
        base(serverUrl) + "/connect"

    public fun mfaSubmit(serverUrl: String, sessionId: String, code: String): String =
        base(serverUrl) + "/mfa/submit?session_id=" + encodeQuery(sessionId) +
            "&code=" + encodeQuery(code)

    /**
     * Best-effort path encoding that matches the Swift counterpart:
     * encodes spaces as %20 and leaves `/` alone (URL paths can contain
     * slashes; we only need to escape spaces and other unsafe chars).
     */
    private fun encodePath(value: String): String {
        return URLEncoder.encode(value, StandardCharsets.UTF_8)
            .replace("+", "%20")
            .replace("%2F", "/")
    }

    private fun encodeQuery(value: String): String =
        URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20")

    /** Validate a URL is well-formed without making it absolute-mandatory. */
    public fun parse(url: String): URI? = try {
        URI(url)
    } catch (e: Exception) {
        null
    }
}
