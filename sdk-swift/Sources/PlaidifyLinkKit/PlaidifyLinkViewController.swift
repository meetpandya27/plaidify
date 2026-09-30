#if canImport(UIKit) && !os(macOS) && !os(watchOS) && !os(tvOS)
import Foundation
import SwiftUI
import UIKit
import WebKit

/// UIKit entrypoint for Plaidify Link.
///
/// By default every institution is shown in the hosted Link page inside a
/// WKWebView — the supported path. The native SwiftUI screens are
/// **experimental** and only used when `experimentalNativeScreens` is true
/// and the institution is listed in `registry`.
///
/// Embedders integrate via:
///
/// ```swift
/// let vc = PlaidifyLinkViewController(
///     hostedConfiguration: config,
///     onEvent: { event in ... }
/// )
/// present(vc, animated: true)
/// ```
///
/// The controller dismisses itself on `CONNECTED` (the event carries the
/// public token) and on `EXIT`; an `ERROR` leaves Link open on its retry
/// screen.
@available(iOS 15.0, *)
public final class PlaidifyLinkViewController: UIViewController {
    public typealias EventHandler = (PlaidifyLinkEvent) -> Void

    public let hostedConfiguration: PlaidifyHostedLinkConfiguration
    public let registry: PlaidifyLinkInstitutionRegistry
    public let experimentalNativeScreens: Bool
    public let onEvent: EventHandler

    private var hostingController: UIHostingController<AnyView>?
    private var hostedWebView: PlaidifyHostedLinkWebView?
    private let client: PlaidifyLinkClient
    private let flow: PlaidifyLinkConnectFlow
    private lazy var nativeSession = PlaidifyLinkNativeSession(client: client, flow: flow)

    /// - Parameters:
    ///   - experimentalNativeScreens: Opt in to the native SwiftUI screens
    ///     for institutions in `registry`. Experimental; defaults to false
    ///     (hosted web view for everything).
    public init(
        hostedConfiguration: PlaidifyHostedLinkConfiguration,
        registry: PlaidifyLinkInstitutionRegistry = PlaidifyLinkInstitutionRegistry(),
        experimentalNativeScreens: Bool = false,
        onEvent: @escaping EventHandler
    ) {
        self.hostedConfiguration = hostedConfiguration
        self.registry = registry
        self.experimentalNativeScreens = experimentalNativeScreens
        self.onEvent = onEvent
        self.client = PlaidifyLinkClient(
            serverURL: hostedConfiguration.serverURL,
            linkToken: hostedConfiguration.token
        )
        self.flow = PlaidifyLinkConnectFlow(registry: registry)
        super.init(nibName: nil, bundle: nil)

        flow.onEvent = { [weak self] event in
            self?.translate(event)
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    public override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        if experimentalNativeScreens {
            showPicker()
        } else {
            presentWebView()
        }
    }

    // MARK: - Presentation

    private func showPicker() {
        let placeholder = AnyView(
            PlaidifyLinkProgressView(title: "Loading institutions…")
        )
        replaceContent(with: placeholder)
        Task { [weak self] in
            guard let self else { return }
            do {
                let response = try await self.client.searchOrganizations(query: nil, limit: 40)
                self.replaceContent(with: AnyView(
                    PlaidifyLinkPickerView(organizations: response.results) { [weak self] org in
                        self?.flow.apply(.selectInstitution(org))
                    }
                ))
            } catch {
                self.flow.apply(.failed(code: nil, message: "\(error)"))
            }
        }
    }

    private func showCredentials(for organization: PlaidifyOrganization) {
        replaceContent(with: AnyView(
            PlaidifyLinkCredentialsView(organization: organization) { [weak self] username, password in
                guard let self else { return }
                self.onEvent(PlaidifyLinkEvent(
                    event: PlaidifyLinkEventName.credentialsSubmitted.rawValue,
                    organizationID: organization.organizationID,
                    organizationName: organization.name,
                    site: organization.site
                ))
                Task { await self.nativeSession.submitCredentials(username: username, password: password) }
            }
        ))
    }

    private func showMFA() {
        let prompt = flow.state.mfaPrompt ?? "Enter the verification code from your provider to continue."
        replaceContent(with: AnyView(
            PlaidifyLinkMFAView(prompt: prompt, mfaType: flow.state.mfaType) { [weak self] code in
                guard let self else { return }
                self.onEvent(PlaidifyLinkEvent(
                    event: PlaidifyLinkEventName.mfaSubmitted.rawValue,
                    site: self.flow.state.organization?.site,
                    sessionID: self.flow.state.sessionID
                ))
                Task { await self.nativeSession.submitMFA(code: code) }
            }
        ))
    }

    private func showProgress(_ title: String) {
        replaceContent(with: AnyView(PlaidifyLinkProgressView(title: title)))
    }

    private func showError(_ message: String) {
        replaceContent(with: AnyView(
            PlaidifyLinkErrorView(message: message) { [weak self] in
                self?.flow.apply(.reset)
            }
        ))
    }

    // MARK: - Hosted web view

    private func presentWebView() {
        guard let hosted = PlaidifyHostedLinkWebView(
            configuration: hostedConfiguration,
            onEvent: { [weak self] event in
                guard let self else { return }
                self.onEvent(event)
                if event.shouldDismissSheet {
                    self.dismiss(animated: true)
                }
            }
        ) else {
            showError("The Plaidify server URL is not valid.")
            return
        }
        let webView = hosted.webView
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.subviews.forEach { $0.removeFromSuperview() }
        children.forEach {
            $0.willMove(toParent: nil)
            $0.removeFromParent()
        }
        hostingController = nil
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
        hostedWebView = hosted
        hosted.load()
    }

    // MARK: - Helpers

    private func replaceContent(with view: AnyView) {
        if let hosting = hostingController {
            hosting.rootView = view
            return
        }
        let hosting = UIHostingController(rootView: view)
        addChild(hosting)
        hosting.view.translatesAutoresizingMaskIntoConstraints = false
        self.view.addSubview(hosting.view)
        NSLayoutConstraint.activate([
            hosting.view.topAnchor.constraint(equalTo: self.view.safeAreaLayoutGuide.topAnchor),
            hosting.view.bottomAnchor.constraint(equalTo: self.view.safeAreaLayoutGuide.bottomAnchor),
            hosting.view.leadingAnchor.constraint(equalTo: self.view.leadingAnchor),
            hosting.view.trailingAnchor.constraint(equalTo: self.view.trailingAnchor),
        ])
        hosting.didMove(toParent: self)
        hostingController = hosting
    }

    private func translate(_ flowEvent: PlaidifyLinkFlowEvent) {
        switch flowEvent {
        case .stepChanged(let step):
            switch step {
            case .picker: showPicker()
            case .credentials:
                if let org = flow.state.organization { showCredentials(for: org) }
            case .connecting: showProgress("Connecting…")
            case .mfa: showMFA()
            case .success: showProgress("Connected.")
            case .error:
                showError(flow.state.lastErrorMessage ?? "Connection failed.")
            case .consent:
                break
            }
        case .institutionSelected(let org):
            onEvent(PlaidifyLinkEvent(
                event: PlaidifyLinkEventName.institutionSelected.rawValue,
                organizationID: org.organizationID,
                organizationName: org.name,
                site: org.site
            ))
        case .mfaRequired(let type, let sessionID):
            onEvent(PlaidifyLinkEvent(
                event: PlaidifyLinkEventName.mfaRequired.rawValue,
                organizationID: flow.state.organization?.organizationID,
                organizationName: flow.state.organization?.name,
                site: flow.state.organization?.site,
                mfaType: type,
                sessionID: sessionID
            ))
        case .connected(let publicToken, let jobID, let site):
            onEvent(PlaidifyLinkEvent(
                event: PlaidifyLinkEventName.connected.rawValue,
                jobID: jobID,
                publicToken: publicToken,
                organizationID: flow.state.organization?.organizationID,
                organizationName: flow.state.organization?.name,
                site: site
            ))
            dismiss(animated: true)
        case .errored(let code, let message):
            // Recoverable: the error screen offers a retry.
            onEvent(PlaidifyLinkEvent(
                event: PlaidifyLinkEventName.error.rawValue,
                organizationID: flow.state.organization?.organizationID,
                organizationName: flow.state.organization?.name,
                site: flow.state.organization?.site,
                error: message,
                errorCode: code
            ))
        case .fallbackToWebView(_, let reason):
            onEvent(PlaidifyLinkEvent(
                event: "FALLBACK_WEBVIEW",
                organizationID: flow.state.organization?.organizationID,
                organizationName: flow.state.organization?.name,
                site: flow.state.organization?.site,
                reason: reason
            ))
            presentWebView()
        }
    }
}
#endif
