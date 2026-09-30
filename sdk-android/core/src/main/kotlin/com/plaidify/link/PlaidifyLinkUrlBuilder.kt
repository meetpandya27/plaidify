package com.plaidify.link

import java.net.URI
import java.net.URLEncoder

/** Embedder branding the hosted page reads from its URL (see frontend-next/src/branding.ts). */
public data class PlaidifyLinkTheme(
    /** Buttons and focus rings, as a hex colour ("#0b8f73"). */
    val accentColor: String? = null,
    /** Page background behind the Link card, as a hex colour. */
    val backgroundColor: String? = null,
    /** Corner radius of the Link card, e.g. "24px". */
    val borderRadius: String? = null,
    /**
     * Logo above every step, as a `data:image/…;base64,` URI (at most
     * 32 KB); the hosted page only loads images from itself and data: URIs.
     */
    val logo: String? = null,
)

/**
 * Pure URL builders for the Plaidify Link REST endpoints. Secrets (codes,
 * credentials) never go in these URLs; they travel in request bodies.
 */
public object PlaidifyLinkUrlBuilder {
    public fun base(serverUrl: String): String =
        if (serverUrl.endsWith("/")) serverUrl.dropLast(1) else serverUrl

    /** The hosted Link page the WebView loads. */
    public fun hostedLink(
        serverUrl: String,
        linkToken: String,
        theme: PlaidifyLinkTheme = PlaidifyLinkTheme(),
    ): String {
        val params = mutableListOf("token=" + encodeQuery(linkToken))
        theme.accentColor?.let { params += "accent=" + encodeQuery(it) }
        theme.backgroundColor?.let { params += "bg=" + encodeQuery(it) }
        theme.borderRadius?.let { params += "radius=" + encodeQuery(it) }
        theme.logo?.let { params += "logo=" + encodeQuery(it) }
        return base(serverUrl) + "/link?" + params.joinToString("&")
    }

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

    public fun mfaSubmit(serverUrl: String): String =
        base(serverUrl) + "/mfa/submit"

    /**
     * Best-effort path encoding that matches the Swift counterpart:
     * encodes spaces as %20 and leaves `/` alone (URL paths can contain
     * slashes; we only need to escape spaces and other unsafe chars).
     */
    private fun encodePath(value: String): String {
        return URLEncoder.encode(value, UTF_8)
            .replace("+", "%20")
            .replace("%2F", "/")
    }

    private fun encodeQuery(value: String): String =
        URLEncoder.encode(value, UTF_8).replace("+", "%20")

    // The charset-name overload: URLEncoder.encode(String, Charset) only
    // exists on Android 13+.
    private const val UTF_8 = "UTF-8"

    /** Validate a URL is well-formed without making it absolute-mandatory. */
    public fun parse(url: String): URI? = try {
        URI(url)
    } catch (e: Exception) {
        null
    }
}
