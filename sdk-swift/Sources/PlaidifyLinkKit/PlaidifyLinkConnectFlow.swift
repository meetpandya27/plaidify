import Foundation

/// Step a hosted-link flow is currently on. Mirrors the React app's
/// reducer so embedders see a consistent state machine.
public enum PlaidifyLinkStep: String, Equatable {
    case consent
    case picker
    case credentials
    case connecting
    case mfa
    case success
    case error
}

/// A single observed callback emitted by the flow. Callers translate
/// these into native UI updates and/or webview bridge events.
public enum PlaidifyLinkFlowEvent: Equatable {
    case stepChanged(PlaidifyLinkStep)
    case institutionSelected(PlaidifyOrganization)
    case mfaRequired(type: String, sessionID: String?)
    case connected(publicToken: String?, jobID: String?, site: String?)
    case errored(code: String?, message: String)
    case fallbackToWebView(PlaidifyOrganization, reason: String)
}

/// Pure state for the connect flow. Holds no UI dependencies so it
/// can be unit-tested under `swift test` without simulator/device.
public struct PlaidifyLinkFlowState: Equatable {
    public var step: PlaidifyLinkStep
    public var organization: PlaidifyOrganization?
    public var sessionID: String?
    public var mfaType: String?
    public var lastErrorCode: String?
    public var lastErrorMessage: String?
    public var publicToken: String?
    public var jobID: String?

    public init(
        step: PlaidifyLinkStep = .picker,
        organization: PlaidifyOrganization? = nil,
        sessionID: String? = nil,
        mfaType: String? = nil,
        lastErrorCode: String? = nil,
        lastErrorMessage: String? = nil,
        publicToken: String? = nil,
        jobID: String? = nil
    ) {
        self.step = step
        self.organization = organization
        self.sessionID = sessionID
        self.mfaType = mfaType
        self.lastErrorCode = lastErrorCode
        self.lastErrorMessage = lastErrorMessage
        self.publicToken = publicToken
        self.jobID = jobID
    }
}

/// Coordinates the picker → credentials → MFA → success transitions.
///
/// The flow is driven by `apply` calls so a UI layer (SwiftUI/UIKit)
/// can dispatch user actions and observe the resulting events without
/// owning any business logic.
public final class PlaidifyLinkConnectFlow {
    public private(set) var state: PlaidifyLinkFlowState
    public let registry: PlaidifyLinkInstitutionRegistry
    public var onEvent: (PlaidifyLinkFlowEvent) -> Void

    public init(
        registry: PlaidifyLinkInstitutionRegistry = PlaidifyLinkInstitutionRegistry(),
        initialState: PlaidifyLinkFlowState = PlaidifyLinkFlowState(),
        onEvent: @escaping (PlaidifyLinkFlowEvent) -> Void = { _ in }
    ) {
        self.registry = registry
        self.state = initialState
        self.onEvent = onEvent
    }

    public enum Action: Equatable {
        case selectInstitution(PlaidifyOrganization)
        case credentialsSubmitted
        case connectResponded(PlaidifyConnectResponse)
        case mfaSubmitted
        case mfaResponded(PlaidifyConnectResponse)
        case failed(code: String?, message: String)
        case reset
    }

    @discardableResult
    public func apply(_ action: Action) -> PlaidifyLinkFlowState {
        switch action {
        case .selectInstitution(let organization):
            switch registry.strategy(for: organization) {
            case .webViewFallback(let reason):
                onEvent(.fallbackToWebView(organization, reason: reason))
                state.organization = organization
            case .native:
                state.organization = organization
                state.step = .credentials
                onEvent(.institutionSelected(organization))
                onEvent(.stepChanged(.credentials))
            }

        case .credentialsSubmitted:
            state.step = .connecting
            onEvent(.stepChanged(.connecting))

        case .connectResponded(let response):
            handleConnectResponse(response)

        case .mfaSubmitted:
            state.step = .connecting
            onEvent(.stepChanged(.connecting))

        case .mfaResponded(let response):
            handleConnectResponse(response)

        case .failed(let code, let message):
            state.step = .error
            state.lastErrorCode = code
            state.lastErrorMessage = message
            onEvent(.errored(code: code, message: message))
            onEvent(.stepChanged(.error))

        case .reset:
            state = PlaidifyLinkFlowState()
            onEvent(.stepChanged(.picker))
        }
        return state
    }

    private func handleConnectResponse(_ response: PlaidifyConnectResponse) {
        switch response.status {
        case "completed":
            state.step = .success
            state.publicToken = response.publicToken
            state.jobID = response.jobID
            onEvent(.connected(
                publicToken: response.publicToken,
                jobID: response.jobID,
                site: state.organization?.site
            ))
            onEvent(.stepChanged(.success))

        case "mfa_required":
            state.step = .mfa
            state.sessionID = response.sessionID
            state.mfaType = response.mfaType
            onEvent(.mfaRequired(type: response.mfaType ?? "otp", sessionID: response.sessionID))
            onEvent(.stepChanged(.mfa))

        case "error":
            apply(.failed(code: nil, message: response.errorMessage ?? response.message ?? "Connection failed."))

        default:
            // pending / unknown: stay on connecting
            break
        }
    }
}
