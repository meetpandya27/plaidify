package com.plaidify.link

/** Step the hosted-link flow is currently on. */
public enum class PlaidifyLinkStep {
    Consent, Picker, Credentials, Connecting, Mfa, Success, Error
}

/** Single observable callback emitted by [PlaidifyLinkConnectFlow]. */
public sealed class PlaidifyLinkFlowEvent {
    public data class StepChanged(val step: PlaidifyLinkStep) : PlaidifyLinkFlowEvent()
    public data class InstitutionSelected(val organization: PlaidifyOrganization) : PlaidifyLinkFlowEvent()
    public data class MfaRequired(val type: String, val sessionId: String?) : PlaidifyLinkFlowEvent()
    public data class Connected(
        val publicToken: String?,
        val jobId: String?,
        val site: String?,
    ) : PlaidifyLinkFlowEvent()

    public data class Errored(val code: String?, val message: String) : PlaidifyLinkFlowEvent()
    public data class FallbackToWebView(
        val organization: PlaidifyOrganization,
        val reason: String,
    ) : PlaidifyLinkFlowEvent()
}

/** Pure state for the connect flow. */
public data class PlaidifyLinkFlowState(
    val step: PlaidifyLinkStep = PlaidifyLinkStep.Picker,
    val organization: PlaidifyOrganization? = null,
    val sessionId: String? = null,
    val mfaType: String? = null,
    val lastErrorCode: String? = null,
    val lastErrorMessage: String? = null,
    val publicToken: String? = null,
    val jobId: String? = null,
)

/**
 * Pure state machine for the picker → credentials → connecting →
 * mfa → success/error transitions. The class is intentionally
 * UI-framework agnostic so it is fully unit-testable on the JVM.
 */
public class PlaidifyLinkConnectFlow(
    public val registry: PlaidifyLinkInstitutionRegistry = PlaidifyLinkInstitutionRegistry(),
    initialState: PlaidifyLinkFlowState = PlaidifyLinkFlowState(),
    public var onEvent: (PlaidifyLinkFlowEvent) -> Unit = {},
) {
    public var state: PlaidifyLinkFlowState = initialState
        private set

    public sealed class Action {
        public data class SelectInstitution(val organization: PlaidifyOrganization) : Action()
        public object CredentialsSubmitted : Action()
        public data class ConnectResponded(val response: PlaidifyConnectResponse) : Action()
        public object MfaSubmitted : Action()
        public data class MfaResponded(val response: PlaidifyConnectResponse) : Action()
        public data class Failed(val code: String?, val message: String) : Action()
        public object Reset : Action()
    }

    public fun apply(action: Action): PlaidifyLinkFlowState {
        when (action) {
            is Action.SelectInstitution -> {
                when (val strategy = registry.strategy(action.organization)) {
                    is PlaidifyLinkInstitutionStrategy.WebViewFallback -> {
                        onEvent(PlaidifyLinkFlowEvent.FallbackToWebView(action.organization, strategy.reason))
                        state = state.copy(organization = action.organization)
                    }
                    PlaidifyLinkInstitutionStrategy.Native -> {
                        state = state.copy(
                            organization = action.organization,
                            step = PlaidifyLinkStep.Credentials,
                        )
                        onEvent(PlaidifyLinkFlowEvent.InstitutionSelected(action.organization))
                        onEvent(PlaidifyLinkFlowEvent.StepChanged(PlaidifyLinkStep.Credentials))
                    }
                }
            }

            Action.CredentialsSubmitted -> {
                state = state.copy(step = PlaidifyLinkStep.Connecting)
                onEvent(PlaidifyLinkFlowEvent.StepChanged(PlaidifyLinkStep.Connecting))
            }

            is Action.ConnectResponded -> handleConnectResponse(action.response)

            Action.MfaSubmitted -> {
                state = state.copy(step = PlaidifyLinkStep.Connecting)
                onEvent(PlaidifyLinkFlowEvent.StepChanged(PlaidifyLinkStep.Connecting))
            }

            is Action.MfaResponded -> handleConnectResponse(action.response)

            is Action.Failed -> {
                state = state.copy(
                    step = PlaidifyLinkStep.Error,
                    lastErrorCode = action.code,
                    lastErrorMessage = action.message,
                )
                onEvent(PlaidifyLinkFlowEvent.Errored(action.code, action.message))
                onEvent(PlaidifyLinkFlowEvent.StepChanged(PlaidifyLinkStep.Error))
            }

            Action.Reset -> {
                state = PlaidifyLinkFlowState()
                onEvent(PlaidifyLinkFlowEvent.StepChanged(PlaidifyLinkStep.Picker))
            }
        }
        return state
    }

    private fun handleConnectResponse(response: PlaidifyConnectResponse) {
        when (response.status) {
            "completed" -> {
                state = state.copy(
                    step = PlaidifyLinkStep.Success,
                    publicToken = response.publicToken,
                    jobId = response.jobId,
                )
                onEvent(
                    PlaidifyLinkFlowEvent.Connected(
                        publicToken = response.publicToken,
                        jobId = response.jobId,
                        site = state.organization?.site,
                    )
                )
                onEvent(PlaidifyLinkFlowEvent.StepChanged(PlaidifyLinkStep.Success))
            }

            "mfa_required" -> {
                state = state.copy(
                    step = PlaidifyLinkStep.Mfa,
                    sessionId = response.sessionId,
                    mfaType = response.mfaType,
                )
                onEvent(
                    PlaidifyLinkFlowEvent.MfaRequired(
                        type = response.mfaType ?: "otp",
                        sessionId = response.sessionId,
                    )
                )
                onEvent(PlaidifyLinkFlowEvent.StepChanged(PlaidifyLinkStep.Mfa))
            }

            "error" -> {
                apply(
                    Action.Failed(
                        code = null,
                        message = response.errorMessage ?: response.message ?: "Connection failed.",
                    )
                )
            }

            else -> {
                // pending / unknown: stay on connecting.
            }
        }
    }
}
