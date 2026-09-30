package com.plaidify.link

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.net.URI

/**
 * The hosted page's bridge contract on Android, and the policy the WebView
 * enforces around it. Framework-free so it is unit-tested on the JVM; the
 * `ui` module's PlaidifyLinkActivity wires it to a WebView.
 *
 * The page calls `window.plaidifyLink.postMessage(json)` with one event as
 * a JSON string (frontend-next/src/config.ts `detectNativeBridges`). The
 * Activity injects that object with `WebViewCompat.addWebMessageListener`,
 * restricted to the Plaidify origin.
 */
public const val PLAIDIFY_BRIDGE_NAME: String = "plaidifyLink"

/** A web origin (scheme, host, port) compared the way browsers do. */
public data class PlaidifyLinkOrigin(
    val scheme: String,
    val host: String,
    /** The effective port; default ports are made explicit. */
    val port: Int,
) {
    public fun matches(url: String?): Boolean = of(url) == this

    /** Whether a bridge message may be trusted: the Plaidify origin's main frame only. */
    public fun acceptsMessage(isMainFrame: Boolean, sourceOrigin: String?): Boolean =
        isMainFrame && matches(sourceOrigin)

    /** The rule for `WebViewCompat.addWebMessageListener`, e.g. "https://api.example.com". */
    public fun toRule(): String {
        val defaultPort = defaultPort(scheme)
        return if (port == defaultPort) "$scheme://$host" else "$scheme://$host:$port"
    }

    public companion object {
        public fun of(url: String?): PlaidifyLinkOrigin? {
            if (url.isNullOrBlank()) return null
            val uri = try {
                URI(url)
            } catch (e: Exception) {
                return null
            }
            val scheme = uri.scheme?.lowercase() ?: return null
            val host = uri.host?.lowercase()?.takeIf { it.isNotEmpty() } ?: return null
            val port = if (uri.port != -1) uri.port else defaultPort(scheme)
            return PlaidifyLinkOrigin(scheme, host, port)
        }

        private fun defaultPort(scheme: String): Int = when (scheme) {
            "https" -> 443
            "http" -> 80
            else -> -1
        }
    }
}

/** What the Link WebView does with a navigation. */
public sealed class PlaidifyLinkNavigation {
    public object Allow : PlaidifyLinkNavigation()
    public object Cancel : PlaidifyLinkNavigation()

    /** Leave the Link WebView and hand the URL to the browser. */
    public data class OpenExternally(val url: String) : PlaidifyLinkNavigation()
}

public object PlaidifyLinkNavigationPolicy {
    private val EXTERNAL_SCHEMES = setOf("http", "https", "mailto", "tel")

    /**
     * Everything but the Plaidify origin stays out of the Link WebView:
     * links the user follows open in the browser; anything else is dropped.
     */
    public fun decide(url: String?, isMainFrame: Boolean, allowed: PlaidifyLinkOrigin): PlaidifyLinkNavigation {
        if (url == null) return PlaidifyLinkNavigation.Cancel
        if (allowed.matches(url)) return PlaidifyLinkNavigation.Allow
        val scheme = try {
            URI(url).scheme?.lowercase()
        } catch (e: Exception) {
            null
        }
        return if (isMainFrame && scheme in EXTERNAL_SCHEMES) {
            PlaidifyLinkNavigation.OpenExternally(url)
        } else {
            PlaidifyLinkNavigation.Cancel
        }
    }
}

/** One hosted-link event, as the page posts it. */
@Serializable
public data class PlaidifyLinkEvent(
    val source: String,
    val event: String,
    @SerialName("public_token") val publicToken: String? = null,
    @SerialName("job_id") val jobId: String? = null,
    val site: String? = null,
    val reason: String? = null,
    val error: String? = null,
    @SerialName("error_code") val errorCode: String? = null,
    @SerialName("mfa_type") val mfaType: String? = null,
    @SerialName("session_id") val sessionId: String? = null,
    @SerialName("organization_id") val organizationId: String? = null,
    @SerialName("organization_name") val organizationName: String? = null,
) {
    /**
     * Link is finished: connected, or the user left. ERROR is not terminal —
     * the page shows retry and choose-another-provider screens after it.
     */
    val isTerminal: Boolean get() = event.uppercase() in TERMINAL_EVENTS

    public companion object {
        private val TERMINAL_EVENTS = setOf("CONNECTED", "EXIT", "DONE", "CLOSE")
        private val json = Json { ignoreUnknownKeys = true }

        /** Parse a bridge message; null for anything that is not a Plaidify Link event. */
        public fun parse(message: String?): PlaidifyLinkEvent? {
            if (message.isNullOrBlank()) return null
            val event = try {
                json.decodeFromString(serializer(), message)
            } catch (e: Exception) {
                return null
            }
            return event.takeIf { it.source == "plaidify-link" }
        }
    }
}

/** How a Link session ended, as handed back to the host app. */
public sealed class PlaidifyLinkResult {
    /** Exchange [publicToken] on your backend (POST /exchange/public_token). */
    public data class Connected(
        val publicToken: String?,
        val jobId: String?,
        val site: String?,
    ) : PlaidifyLinkResult()

    public data class Exited(
        val reason: String?,
        val errorCode: String?,
    ) : PlaidifyLinkResult()

    public companion object {
        /** The result a terminal event ends Link with; null while Link goes on. */
        public fun from(event: PlaidifyLinkEvent): PlaidifyLinkResult? = when {
            !event.isTerminal -> null
            event.event.uppercase() == "CONNECTED" -> Connected(event.publicToken, event.jobId, event.site)
            else -> Exited(event.reason ?: event.event.lowercase(), event.errorCode)
        }
    }
}
