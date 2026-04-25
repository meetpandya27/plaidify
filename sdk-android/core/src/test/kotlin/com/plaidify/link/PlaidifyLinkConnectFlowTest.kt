package com.plaidify.link

import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class PlaidifyLinkConnectFlowTest {
    private fun makeOrg(
        site: String = "rbc",
        authStyle: String? = "username_password",
    ) = PlaidifyOrganization(
        organizationId = "org_$site",
        name = "Test $site",
        site = site,
        authStyle = authStyle,
    )

    private fun nativeRegistry() = PlaidifyLinkInstitutionRegistry(
        supportedSites = setOf("rbc"),
        supportedAuthStyles = setOf("username_password"),
    )

    @Test
    fun happyPathPickerToCredentialsToConnectingToSuccess() {
        val events = mutableListOf<PlaidifyLinkFlowEvent>()
        val flow = PlaidifyLinkConnectFlow(nativeRegistry()) { events += it }

        val org = makeOrg()
        flow.apply(PlaidifyLinkConnectFlow.Action.SelectInstitution(org))
        assertEquals(PlaidifyLinkStep.Credentials, flow.state.step)
        assertEquals(org, flow.state.organization)

        flow.apply(PlaidifyLinkConnectFlow.Action.CredentialsSubmitted)
        assertEquals(PlaidifyLinkStep.Connecting, flow.state.step)

        flow.apply(
            PlaidifyLinkConnectFlow.Action.ConnectResponded(
                PlaidifyConnectResponse(status = "completed", publicToken = "public-1", jobId = "job-1"),
            )
        )
        assertEquals(PlaidifyLinkStep.Success, flow.state.step)
        assertEquals("public-1", flow.state.publicToken)
        assertTrue(events.any { it is PlaidifyLinkFlowEvent.Connected && it.publicToken == "public-1" })
    }

    @Test
    fun mfaResponseTransitionsAndIncludesType() {
        val flow = PlaidifyLinkConnectFlow(nativeRegistry())
        flow.apply(PlaidifyLinkConnectFlow.Action.SelectInstitution(makeOrg()))
        flow.apply(PlaidifyLinkConnectFlow.Action.CredentialsSubmitted)
        flow.apply(
            PlaidifyLinkConnectFlow.Action.ConnectResponded(
                PlaidifyConnectResponse(status = "mfa_required", sessionId = "sess-9", mfaType = "otp"),
            )
        )
        assertEquals(PlaidifyLinkStep.Mfa, flow.state.step)
        assertEquals("sess-9", flow.state.sessionId)
        assertEquals("otp", flow.state.mfaType)
    }

    @Test
    fun fallbackEmittedForUnsupportedAuthStyle() {
        val events = mutableListOf<PlaidifyLinkFlowEvent>()
        val flow = PlaidifyLinkConnectFlow(nativeRegistry()) { events += it }

        val oauthOrg = makeOrg(authStyle = "oauth_redirect")
        flow.apply(PlaidifyLinkConnectFlow.Action.SelectInstitution(oauthOrg))

        assertNotEquals(PlaidifyLinkStep.Credentials, flow.state.step)
        assertTrue(events.any {
            it is PlaidifyLinkFlowEvent.FallbackToWebView
                && it.organization == oauthOrg
                && it.reason.startsWith("auth_style_unsupported")
        })
    }

    @Test
    fun fallbackForSiteNotInRegistry() {
        val events = mutableListOf<PlaidifyLinkFlowEvent>()
        val flow = PlaidifyLinkConnectFlow(nativeRegistry()) { events += it }
        flow.apply(PlaidifyLinkConnectFlow.Action.SelectInstitution(makeOrg(site = "td")))
        assertTrue(events.any {
            it is PlaidifyLinkFlowEvent.FallbackToWebView && it.reason == "site_not_in_native_registry"
        })
    }

    @Test
    fun errorResponseTransitionsToErrorStep() {
        val flow = PlaidifyLinkConnectFlow(nativeRegistry())
        flow.apply(PlaidifyLinkConnectFlow.Action.SelectInstitution(makeOrg()))
        flow.apply(PlaidifyLinkConnectFlow.Action.CredentialsSubmitted)
        flow.apply(
            PlaidifyLinkConnectFlow.Action.ConnectResponded(
                PlaidifyConnectResponse(status = "error", errorMessage = "bad credentials"),
            )
        )
        assertEquals(PlaidifyLinkStep.Error, flow.state.step)
        assertEquals("bad credentials", flow.state.lastErrorMessage)
    }

    @Test
    fun resetReturnsToPicker() {
        val flow = PlaidifyLinkConnectFlow(nativeRegistry())
        flow.apply(PlaidifyLinkConnectFlow.Action.SelectInstitution(makeOrg()))
        flow.apply(PlaidifyLinkConnectFlow.Action.Reset)
        assertEquals(PlaidifyLinkStep.Picker, flow.state.step)
        assertNull(flow.state.organization)
    }

    @Test
    fun emptyRegistryFallsBackForUnknownAuthStyle() {
        val registry = PlaidifyLinkInstitutionRegistry()
        val strategy = registry.strategy(
            PlaidifyOrganization(organizationId = "1", name = "X", site = "x", authStyle = null)
        )
        assertTrue(strategy is PlaidifyLinkInstitutionStrategy.WebViewFallback)
    }
}
