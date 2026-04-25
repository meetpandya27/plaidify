package com.plaidify.link.ui

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import com.plaidify.link.PlaidifyLinkClient
import com.plaidify.link.PlaidifyLinkConnectFlow
import com.plaidify.link.PlaidifyLinkFlowEvent
import com.plaidify.link.PlaidifyLinkInstitutionRegistry
import com.plaidify.link.PlaidifyLinkStep
import com.plaidify.link.PlaidifyOrganization
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * Drop-in Activity that hosts the native Compose Link flow and falls
 * back to a WebView for institutions outside the registry. Equivalent
 * of `PlaidifyLinkViewController` on iOS.
 *
 * Launch:
 * ```
 * val intent = PlaidifyLinkActivity.intent(
 *     context = ctx,
 *     serverUrl = "https://api.example.com",
 *     linkToken = "lnk-abc",
 * )
 * startActivity(intent)
 * ```
 *
 * Embedders observe events via the broadcast queue or by hosting the
 * flow themselves. For the most flexibility, depend directly on
 * `PlaidifyLinkConnectFlow` from the `core` module and compose the UI
 * in your own Activity / NavHost.
 */
public class PlaidifyLinkActivity : ComponentActivity() {

    public companion object {
        public const val EXTRA_SERVER_URL: String = "com.plaidify.link.SERVER_URL"
        public const val EXTRA_LINK_TOKEN: String = "com.plaidify.link.LINK_TOKEN"

        public fun intent(context: Context, serverUrl: String, linkToken: String): Intent =
            Intent(context, PlaidifyLinkActivity::class.java)
                .putExtra(EXTRA_SERVER_URL, serverUrl)
                .putExtra(EXTRA_LINK_TOKEN, linkToken)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val serverUrl = intent.getStringExtra(EXTRA_SERVER_URL).orEmpty()
        val linkToken = intent.getStringExtra(EXTRA_LINK_TOKEN).orEmpty()
        val client = PlaidifyLinkClient(
            serverUrl = serverUrl,
            linkToken = linkToken,
            http = OkHttpAdapter(),
        )
        val flow = PlaidifyLinkConnectFlow(PlaidifyLinkInstitutionRegistry())
        setContent {
            PlaidifyLinkRoot(
                client = client,
                flow = flow,
                hostedLinkUrl = "$serverUrl/link?token=$linkToken",
                onFinished = { finish() },
            )
        }
    }
}

@Composable
private fun PlaidifyLinkRoot(
    client: PlaidifyLinkClient,
    flow: PlaidifyLinkConnectFlow,
    hostedLinkUrl: String,
    onFinished: () -> Unit,
) {
    var organizations by remember { mutableStateOf<List<PlaidifyOrganization>>(emptyList()) }
    var fallbackOrg by remember { mutableStateOf<PlaidifyOrganization?>(null) }
    var step by remember { mutableStateOf(flow.state.step) }
    val scope = rememberCoroutineScopeCompat()

    flow.onEvent = { event ->
        when (event) {
            is PlaidifyLinkFlowEvent.StepChanged -> step = event.step
            is PlaidifyLinkFlowEvent.FallbackToWebView -> fallbackOrg = event.organization
            is PlaidifyLinkFlowEvent.Connected -> onFinished()
            else -> Unit
        }
    }

    LaunchedEffect(Unit) {
        scope.launch {
            organizations = withContext(Dispatchers.IO) {
                runCatching { client.searchOrganizations().organizations }.getOrDefault(emptyList())
            }
        }
    }

    if (fallbackOrg != null) {
        WebViewFallback(hostedLinkUrl = hostedLinkUrl, onFinished = onFinished)
        return
    }

    when (step) {
        PlaidifyLinkStep.Picker ->
            PlaidifyLinkPicker(organizations = organizations) { org ->
                flow.apply(PlaidifyLinkConnectFlow.Action.SelectInstitution(org))
            }

        PlaidifyLinkStep.Credentials -> {
            val org = flow.state.organization ?: return
            PlaidifyLinkCredentials(organization = org) { _, _ ->
                flow.apply(PlaidifyLinkConnectFlow.Action.CredentialsSubmitted)
            }
        }

        PlaidifyLinkStep.Connecting -> PlaidifyLinkProgress(title = "Connecting…")
        PlaidifyLinkStep.Mfa -> PlaidifyLinkMfa(
            prompt = "Enter the verification code from your provider to continue."
        ) { _ -> flow.apply(PlaidifyLinkConnectFlow.Action.MfaSubmitted) }

        PlaidifyLinkStep.Success -> PlaidifyLinkProgress(title = "Connected.")
        PlaidifyLinkStep.Error -> PlaidifyLinkErrorScreen(
            message = flow.state.lastErrorMessage ?: "Connection failed."
        ) { flow.apply(PlaidifyLinkConnectFlow.Action.Reset) }

        PlaidifyLinkStep.Consent -> Unit
    }
}

/**
 * Lightweight wrapper around `androidx.compose.runtime.rememberCoroutineScope`
 * so this file does not require the optional alias import.
 */
@Composable
private fun rememberCoroutineScopeCompat() = androidx.compose.runtime.rememberCoroutineScope()

/**
 * WebView fallback that loads the hosted /link page and forwards
 * postMessage events to the host via the `plaidifyLink` JS interface
 * (matches the React app's bridge contract).
 */
@Composable
private fun WebViewFallback(hostedLinkUrl: String, onFinished: () -> Unit) {
    val context = LocalContext.current
    AndroidView(factory = {
        WebView(context).apply {
            settings.javaScriptEnabled = true
            webViewClient = WebViewClient()
            addJavascriptInterface(object {
                @JavascriptInterface
                fun postMessage(payload: String) {
                    val obj = runCatching { JSONObject(payload) }.getOrNull() ?: return
                    if (obj.optString("source") != "plaidify-link") return
                    val event = obj.optString("event")
                    if (event == "CONNECTED" || event == "EXIT" || event == "ERROR") {
                        onFinished()
                    }
                }
            }, "plaidifyLink")
            loadUrl(Uri.parse(hostedLinkUrl).toString())
        }
    })
}

/** Stub HTTP client. Host apps inject their own (OkHttp/Ktor/etc). */
private class OkHttpAdapter : com.plaidify.link.PlaidifyLinkHttpClient {
    override suspend fun execute(
        request: com.plaidify.link.PlaidifyLinkHttpClient.HttpRequest
    ): com.plaidify.link.PlaidifyLinkHttpClient.HttpResponse {
        throw UnsupportedOperationException(
            "Replace OkHttpAdapter with an OkHttp / Ktor backed implementation in the host app."
        )
    }
}
