package com.plaidify.link.ui

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Looper
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContract
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.viewinterop.AndroidView
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.plaidify.link.PLAIDIFY_BRIDGE_NAME
import com.plaidify.link.PlaidifyLinkClient
import com.plaidify.link.PlaidifyLinkConnectFlow
import com.plaidify.link.PlaidifyLinkEvent
import com.plaidify.link.PlaidifyLinkFlowEvent
import com.plaidify.link.PlaidifyLinkInstitutionRegistry
import com.plaidify.link.PlaidifyLinkNativeSession
import com.plaidify.link.PlaidifyLinkNavigation
import com.plaidify.link.PlaidifyLinkNavigationPolicy
import com.plaidify.link.PlaidifyLinkOrigin
import com.plaidify.link.PlaidifyLinkResult
import com.plaidify.link.PlaidifyLinkStep
import com.plaidify.link.PlaidifyLinkTheme
import com.plaidify.link.PlaidifyLinkUrlBuilder
import com.plaidify.link.PlaidifyOrganization
import kotlinx.coroutines.launch

/**
 * Drop-in Activity for Plaidify Link. Equivalent of
 * `PlaidifyLinkViewController` on iOS.
 *
 * Every institution is shown in the hosted Link page inside a WebView — the
 * supported path. The native Compose screens are **experimental** and only
 * used with `experimentalNativeScreens = true`, for institutions listed in
 * the registry.
 *
 * The outcome comes back as the Activity result:
 * ```
 * private val link = registerForActivityResult(PlaidifyLinkActivity.Contract()) { result ->
 *     when (result) {
 *         is PlaidifyLinkResult.Connected -> exchangeOnBackend(result.publicToken)
 *         is PlaidifyLinkResult.Exited -> Unit
 *     }
 * }
 * link.launch(PlaidifyLinkActivity.Contract.Input(serverUrl, linkToken))
 * ```
 * Link finishes on `CONNECTED` (RESULT_OK with [EXTRA_PUBLIC_TOKEN]) or
 * `EXIT` (RESULT_CANCELED with [EXTRA_EXIT_REASON]); an `ERROR` leaves it
 * open on its retry screen.
 */
public class PlaidifyLinkActivity : ComponentActivity() {

    public companion object {
        public const val EXTRA_SERVER_URL: String = "com.plaidify.link.SERVER_URL"
        public const val EXTRA_LINK_TOKEN: String = "com.plaidify.link.LINK_TOKEN"
        public const val EXTRA_EXPERIMENTAL_NATIVE_SCREENS: String = "com.plaidify.link.EXPERIMENTAL_NATIVE_SCREENS"
        public const val EXTRA_NATIVE_SITES: String = "com.plaidify.link.NATIVE_SITES"
        public const val EXTRA_THEME_ACCENT: String = "com.plaidify.link.THEME_ACCENT"
        public const val EXTRA_THEME_BACKGROUND: String = "com.plaidify.link.THEME_BACKGROUND"
        public const val EXTRA_THEME_RADIUS: String = "com.plaidify.link.THEME_RADIUS"
        public const val EXTRA_THEME_LOGO: String = "com.plaidify.link.THEME_LOGO"

        /** Result extras. */
        public const val EXTRA_PUBLIC_TOKEN: String = "com.plaidify.link.PUBLIC_TOKEN"
        public const val EXTRA_JOB_ID: String = "com.plaidify.link.JOB_ID"
        public const val EXTRA_SITE: String = "com.plaidify.link.SITE"
        public const val EXTRA_EXIT_REASON: String = "com.plaidify.link.EXIT_REASON"
        public const val EXTRA_ERROR_CODE: String = "com.plaidify.link.ERROR_CODE"

        public fun intent(
            context: Context,
            serverUrl: String,
            linkToken: String,
            theme: PlaidifyLinkTheme = PlaidifyLinkTheme(),
            experimentalNativeScreens: Boolean = false,
            /** Sites the experimental native screens may handle; all others use the WebView. */
            nativeSites: Set<String> = emptySet(),
        ): Intent =
            Intent(context, PlaidifyLinkActivity::class.java)
                .putExtra(EXTRA_SERVER_URL, serverUrl)
                .putExtra(EXTRA_LINK_TOKEN, linkToken)
                .putExtra(EXTRA_THEME_ACCENT, theme.accentColor)
                .putExtra(EXTRA_THEME_BACKGROUND, theme.backgroundColor)
                .putExtra(EXTRA_THEME_RADIUS, theme.borderRadius)
                .putExtra(EXTRA_THEME_LOGO, theme.logo)
                .putExtra(EXTRA_EXPERIMENTAL_NATIVE_SCREENS, experimentalNativeScreens)
                .putExtra(EXTRA_NATIVE_SITES, nativeSites.toTypedArray())

        /** Read the Activity result back into a [PlaidifyLinkResult]. */
        public fun parseResult(resultCode: Int, data: Intent?): PlaidifyLinkResult =
            if (resultCode == Activity.RESULT_OK) {
                PlaidifyLinkResult.Connected(
                    publicToken = data?.getStringExtra(EXTRA_PUBLIC_TOKEN),
                    jobId = data?.getStringExtra(EXTRA_JOB_ID),
                    site = data?.getStringExtra(EXTRA_SITE),
                )
            } else {
                PlaidifyLinkResult.Exited(
                    // No data: the user backed out before Link said anything.
                    reason = data?.getStringExtra(EXTRA_EXIT_REASON) ?: "user_closed",
                    errorCode = data?.getStringExtra(EXTRA_ERROR_CODE),
                )
            }
    }

    /** `registerForActivityResult(PlaidifyLinkActivity.Contract()) { result -> ... }` */
    public class Contract : ActivityResultContract<Contract.Input, PlaidifyLinkResult>() {
        public data class Input(
            val serverUrl: String,
            val linkToken: String,
            val theme: PlaidifyLinkTheme = PlaidifyLinkTheme(),
            val experimentalNativeScreens: Boolean = false,
            val nativeSites: Set<String> = emptySet(),
        )

        override fun createIntent(context: Context, input: Input): Intent =
            intent(
                context,
                input.serverUrl,
                input.linkToken,
                input.theme,
                input.experimentalNativeScreens,
                input.nativeSites,
            )

        override fun parseResult(resultCode: Int, intent: Intent?): PlaidifyLinkResult =
            PlaidifyLinkActivity.parseResult(resultCode, intent)
    }

    private var webView: WebView? = null
    private var finished = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val serverUrl = intent.getStringExtra(EXTRA_SERVER_URL).orEmpty()
        val linkToken = intent.getStringExtra(EXTRA_LINK_TOKEN).orEmpty()
        val origin = PlaidifyLinkOrigin.of(serverUrl)
        if (origin == null || linkToken.isEmpty()) {
            finishWith(PlaidifyLinkResult.Exited(reason = "invalid_configuration", errorCode = null))
            return
        }
        val theme = PlaidifyLinkTheme(
            accentColor = intent.getStringExtra(EXTRA_THEME_ACCENT),
            backgroundColor = intent.getStringExtra(EXTRA_THEME_BACKGROUND),
            borderRadius = intent.getStringExtra(EXTRA_THEME_RADIUS),
            logo = intent.getStringExtra(EXTRA_THEME_LOGO),
        )
        val hostedLinkUrl = PlaidifyLinkUrlBuilder.hostedLink(serverUrl, linkToken, theme)

        if (!intent.getBooleanExtra(EXTRA_EXPERIMENTAL_NATIVE_SCREENS, false)) {
            setContentView(linkWebView(hostedLinkUrl, origin))
            return
        }

        val client = PlaidifyLinkClient(serverUrl = serverUrl, linkToken = linkToken)
        val nativeSites = intent.getStringArrayExtra(EXTRA_NATIVE_SITES)?.toSet().orEmpty()
        val flow = PlaidifyLinkConnectFlow(PlaidifyLinkInstitutionRegistry(supportedSites = nativeSites))
        val session = PlaidifyLinkNativeSession(client, flow)
        setContent {
            NativeLinkRoot(
                client = client,
                session = session,
                webView = { linkWebView(hostedLinkUrl, origin) },
                onResult = ::finishWith,
            )
        }
    }

    override fun onDestroy() {
        webView?.apply {
            stopLoading()
            destroy()
        }
        webView = null
        super.onDestroy()
    }

    /** Hand the outcome to the caller once, then close. */
    internal fun finishWith(result: PlaidifyLinkResult) {
        if (finished) return
        finished = true
        when (result) {
            is PlaidifyLinkResult.Connected -> setResult(
                Activity.RESULT_OK,
                Intent()
                    .putExtra(EXTRA_PUBLIC_TOKEN, result.publicToken)
                    .putExtra(EXTRA_JOB_ID, result.jobId)
                    .putExtra(EXTRA_SITE, result.site),
            )
            is PlaidifyLinkResult.Exited -> setResult(
                Activity.RESULT_CANCELED,
                Intent()
                    .putExtra(EXTRA_EXIT_REASON, result.reason)
                    .putExtra(EXTRA_ERROR_CODE, result.errorCode),
            )
        }
        finish()
    }

    /**
     * The hosted Link page, locked to [origin]: bridge messages are accepted
     * only from that origin's main frame, and any other link opens in the
     * browser instead of inside Link.
     */
    @SuppressLint("SetJavaScriptEnabled")
    private fun linkWebView(url: String, origin: PlaidifyLinkOrigin): WebView {
        webView?.let { return it }
        val view = WebView(this)
        view.settings.apply {
            javaScriptEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            javaScriptCanOpenWindowsAutomatically = false
            setSupportMultipleWindows(false)
            setGeolocationEnabled(false)
        }
        view.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean =
                when (val decision = PlaidifyLinkNavigationPolicy.decide(request.url.toString(), request.isForMainFrame, origin)) {
                    PlaidifyLinkNavigation.Allow -> false
                    PlaidifyLinkNavigation.Cancel -> true
                    is PlaidifyLinkNavigation.OpenExternally -> {
                        runCatching {
                            startActivity(
                                Intent(Intent.ACTION_VIEW, Uri.parse(decision.url))
                                    .addCategory(Intent.CATEGORY_BROWSABLE)
                            )
                        }
                        true
                    }
                }
        }

        val onMessage: (String?) -> Unit = { message ->
            val event = PlaidifyLinkEvent.parse(message)
            // CONNECTED and EXIT end Link; ERROR and the rest do not.
            event?.let { PlaidifyLinkResult.from(it) }?.let(::finishWith)
        }
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            // Injected only into frames on the Plaidify origin; the callback
            // also reports the sender's origin and whether it is the main frame.
            WebViewCompat.addWebMessageListener(
                view,
                PLAIDIFY_BRIDGE_NAME,
                setOf(origin.toRule()),
            ) { _, message, sourceOrigin, isMainFrame, _ ->
                if (origin.acceptsMessage(isMainFrame, sourceOrigin.toString())) {
                    onMainThread(view) { onMessage(message.data) }
                }
            }
        } else {
            // WebView < 86. A JavaScript interface is visible to every frame
            // and page and is called off the main thread, so hop to the main
            // thread and check the WebView is still on the Plaidify page.
            // Frames are not an issue there: the page's CSP only allows
            // frames from its own origin.
            view.addJavascriptInterface(
                object {
                    @JavascriptInterface
                    fun postMessage(payload: String) {
                        view.post {
                            if (origin.matches(view.url)) onMessage(payload)
                        }
                    }
                },
                PLAIDIFY_BRIDGE_NAME,
            )
        }
        view.loadUrl(url)
        webView = view
        return view
    }

    private fun onMainThread(view: WebView, block: () -> Unit) {
        if (Looper.myLooper() == Looper.getMainLooper()) block() else view.post { block() }
    }
}

/** The experimental native screens, falling back to the hosted page per institution. */
@Composable
private fun NativeLinkRoot(
    client: PlaidifyLinkClient,
    session: PlaidifyLinkNativeSession,
    webView: () -> WebView,
    onResult: (PlaidifyLinkResult) -> Unit,
) {
    val flow = session.flow
    var organizations by remember { mutableStateOf<List<PlaidifyOrganization>>(emptyList()) }
    var showWebView by remember { mutableStateOf(false) }
    var step by remember { mutableStateOf(flow.state.step) }
    val scope = rememberCoroutineScope()

    flow.onEvent = { event ->
        when (event) {
            is PlaidifyLinkFlowEvent.StepChanged -> step = event.step
            is PlaidifyLinkFlowEvent.FallbackToWebView -> showWebView = true
            is PlaidifyLinkFlowEvent.Connected ->
                onResult(PlaidifyLinkResult.Connected(event.publicToken, event.jobId, event.site))
            else -> Unit
        }
    }

    LaunchedEffect(Unit) {
        organizations = runCatching { client.searchOrganizations().results }.getOrDefault(emptyList())
    }

    if (showWebView) {
        AndroidView(factory = { webView() })
        return
    }

    when (step) {
        PlaidifyLinkStep.Picker ->
            PlaidifyLinkPicker(organizations = organizations) { org ->
                flow.apply(PlaidifyLinkConnectFlow.Action.SelectInstitution(org))
            }

        PlaidifyLinkStep.Credentials -> {
            val org = flow.state.organization ?: return
            PlaidifyLinkCredentials(organization = org) { username, password ->
                scope.launch { session.submitCredentials(username, password) }
            }
        }

        PlaidifyLinkStep.Connecting -> PlaidifyLinkProgress(title = "Connecting…")
        PlaidifyLinkStep.Mfa -> PlaidifyLinkMfa(
            prompt = flow.state.mfaPrompt ?: "Enter the verification code from your provider to continue.",
            mfaType = flow.state.mfaType,
        ) { code -> scope.launch { session.submitMfa(code) } }

        PlaidifyLinkStep.Success -> PlaidifyLinkProgress(title = "Connected.")
        PlaidifyLinkStep.Error -> PlaidifyLinkErrorScreen(
            message = flow.state.lastErrorMessage ?: "Connection failed."
        ) { flow.apply(PlaidifyLinkConnectFlow.Action.Reset) }

        PlaidifyLinkStep.Consent -> Unit
    }
}
