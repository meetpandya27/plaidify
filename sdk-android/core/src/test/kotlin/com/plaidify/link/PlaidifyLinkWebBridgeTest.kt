package com.plaidify.link

import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class PlaidifyLinkWebBridgeTest {
    private val origin = assertNotNull(PlaidifyLinkOrigin.of("https://api.example.com/link?token=x"))

    @Test
    fun bridgeAcceptsOnlyThePlaidifyMainFrame() {
        assertTrue(origin.acceptsMessage(isMainFrame = true, sourceOrigin = "https://api.example.com"))
        assertTrue(origin.acceptsMessage(isMainFrame = true, sourceOrigin = "https://API.example.com:443"))
        // A frame inside the page, even on the same origin.
        assertFalse(origin.acceptsMessage(isMainFrame = false, sourceOrigin = "https://api.example.com"))
        // The WebView navigated somewhere else.
        assertFalse(origin.acceptsMessage(isMainFrame = true, sourceOrigin = "https://evil.example"))
        assertFalse(origin.acceptsMessage(isMainFrame = true, sourceOrigin = "http://api.example.com"))
        assertFalse(origin.acceptsMessage(isMainFrame = true, sourceOrigin = "https://api.example.com:8443"))
        assertFalse(origin.acceptsMessage(isMainFrame = true, sourceOrigin = null))
    }

    @Test
    fun originRuleForAddWebMessageListener() {
        assertEquals("https://api.example.com", origin.toRule())
        assertEquals("http://10.0.2.2:8000", PlaidifyLinkOrigin.of("http://10.0.2.2:8000/")?.toRule())
    }

    @Test
    fun navigationStaysOnThePlaidifyOrigin() {
        val decide = { url: String?, mainFrame: Boolean -> PlaidifyLinkNavigationPolicy.decide(url, mainFrame, origin) }

        assertEquals(PlaidifyLinkNavigation.Allow, decide("https://api.example.com/link?token=abc", true))
        assertEquals(PlaidifyLinkNavigation.Allow, decide("https://api.example.com/ui-next/assets/app.js", false))
        assertEquals(
            PlaidifyLinkNavigation.OpenExternally("https://bank.example/help"),
            decide("https://bank.example/help", true),
        )
        assertEquals(PlaidifyLinkNavigation.Cancel, decide("https://tracker.example/frame", false))
        assertEquals(PlaidifyLinkNavigation.Cancel, decide("javascript:alert(1)", true))
        assertEquals(PlaidifyLinkNavigation.Cancel, decide("file:///sdcard/secret", true))
        assertEquals(PlaidifyLinkNavigation.Cancel, decide("intent://scan/#Intent;scheme=zxing;end", true))
        assertEquals(PlaidifyLinkNavigation.Cancel, decide(null, true))
    }

    @Test
    fun parsesOnlyPlaidifyEvents() {
        val event = assertNotNull(
            PlaidifyLinkEvent.parse(
                """{"source":"plaidify-link","event":"CONNECTED","public_token":"public-1","job_id":"job-1","site":"hydro_one","data":{"x":1}}"""
            )
        )
        assertEquals("public-1", event.publicToken)
        assertNull(PlaidifyLinkEvent.parse("""{"source":"other","event":"CONNECTED"}"""))
        assertNull(PlaidifyLinkEvent.parse("not json"))
        assertNull(PlaidifyLinkEvent.parse(null))
    }

    @Test
    fun connectedHandsBackThePublicToken() {
        val event = assertNotNull(
            PlaidifyLinkEvent.parse("""{"source":"plaidify-link","event":"CONNECTED","public_token":"public-1","job_id":"job-1","site":"hydro_one"}""")
        )
        assertEquals(PlaidifyLinkResult.Connected("public-1", "job-1", "hydro_one"), PlaidifyLinkResult.from(event))
    }

    @Test
    fun exitHandsBackItsReasonAndLastError() {
        val event = assertNotNull(
            PlaidifyLinkEvent.parse("""{"source":"plaidify-link","event":"EXIT","reason":"user_exit","error_code":"rate_limited"}""")
        )
        assertEquals(PlaidifyLinkResult.Exited("user_exit", "rate_limited"), PlaidifyLinkResult.from(event))
    }

    @Test
    fun errorIsNotTheEnd() {
        // The page shows retry / choose-another-provider after an ERROR.
        val event = assertNotNull(
            PlaidifyLinkEvent.parse("""{"source":"plaidify-link","event":"ERROR","error":"bad password","error_code":"invalid_credentials"}""")
        )
        assertFalse(event.isTerminal)
        assertNull(PlaidifyLinkResult.from(event))
        listOf("MFA_REQUIRED", "OPEN", "TELEMETRY", "INSTITUTION_SELECTED").forEach { name ->
            assertNull(PlaidifyLinkResult.from(PlaidifyLinkEvent(source = "plaidify-link", event = name)))
        }
    }
}
