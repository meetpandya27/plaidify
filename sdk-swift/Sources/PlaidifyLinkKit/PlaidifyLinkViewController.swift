#if canImport(UIKit) && !os(macOS) && !os(watchOS) && !os(tvOS)
import Foundation
import SwiftUI
import UIKit
import WebKit

/// UIKit entrypoint that hosts the native Plaidify Link flow and falls
/// back to the existing WKWebView surface when an institution isn't
/// covered by the native UI yet.
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
@available(iOS 15.0, *)
public final class PlaidifyLinkViewController: UIViewController {
    public typealias EventHandler = (PlaidifyLinkEvent) -> Void

    public let hostedConfiguration: PlaidifyHostedLinkConfiguration
    public let registry: PlaidifyLinkInstitutionRegistry
    public let onEvent: EventHandler

    private var hostingController: UIHostingController<AnyView>?
    private var webView: WKWebView?
    private var messageHandler: PlaidifyLinkScriptMessageHandler?
    private let client: PlaidifyLinkClient
    private let flow: PlaidifyLinkConnectFlow

    public init(
        hostedConfiguration: PlaidifyHostedLinkConfiguration,
        registry: PlaidifyLinkInstitutionRegistry = PlaidifyLinkInstitutionRegistry(),
        onEvent: @escaping EventHandler
    ) {
        self.hostedConfiguration = hostedConfiguration
        self.registry = registry
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
        showPicker()
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
                await MainActor.run {
                    self.replaceContent(with: AnyView(
                        PlaidifyLinkPickerView(organizations: response.organizations) { org in
                            self.flow.apply(.selectInstitution(org))
                        }
                    ))
                }
            } catch {
                await MainActor.run {
                    self.flow.apply(.failed(code: nil, message: "\(error)"))
                }
            }
        }
    }

    private func showCredentials(for organization: PlaidifyOrganization) {
        replaceContent(with: AnyView(
            PlaidifyLinkCredentialsView(organization: organization) { [weak self] _, _ in
                // Encryption + connect happen at the app integration layer
                // (the SDK does not bundle WebCrypto-equivalent code).
                // We expose `CREDENTIALS_SUBMITTED` and let the host app
                // post-process and call back into the flow with the
                // connect response.
                self?.onEvent(PlaidifyLinkEvent(
                    source: "plaidify-link",
                    event: PlaidifyLinkEventName.credentialsSubmitted.rawValue,
                    jobID: nil,
                    publicToken: nil,
                    organizationID: organization.organizationID,
                    organizationName: organization.name,
                    site: organization.site,
                    mfaType: nil,
                    sessionID: nil,
                    error: nil,
                    reason: nil
                ))
                self?.flow.apply(.credentialsSubmitted)
            }
        ))
    }

    private func showMFA() {
        let prompt = "Enter the verification code from your provider to continue."
        replaceContent(with: AnyView(
            PlaidifyLinkMFAView(prompt: prompt) { [weak self] _ in
                self?.flow.apply(.mfaSubmitted)
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
                self?.showPicker()
            }
        ))
    }

    // MARK: - Webview fallback

    private func presentWebViewFallback() {
        let handler = PlaidifyLinkScriptMessageHandler { [weak self] event in
            self?.onEvent(event)
            if event.shouldDismissSheet {
                self?.dismiss(animated: true)
            }
        }
        let webView = PlaidifyLinkWebViewFactory.makeWebView(
            hostedLink: hostedConfiguration,
            messageHandler: handler
        )
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.subviews.forEach { $0.removeFromSuperview() }
        children.forEach {
            $0.willMove(toParent: nil)
            $0.removeFromParent()
        }
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
        self.webView = webView
        self.messageHandler = handler
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
                source: "plaidify-link",
                event: PlaidifyLinkEventName.institutionSelected.rawValue,
                jobID: nil, publicToken: nil,
                organizationID: org.organizationID,
                organizationName: org.name, site: org.site,
                mfaType: nil, sessionID: nil, error: nil, reason: nil
            ))
        case .mfaRequired(let type, let sessionID):
            onEvent(PlaidifyLinkEvent(
                source: "plaidify-link",
                event: PlaidifyLinkEventName.mfaRequired.rawValue,
                jobID: nil, publicToken: nil,
                organizationID: flow.state.organization?.organizationID,
                organizationName: flow.state.organization?.name,
                site: flow.state.organization?.site,
                mfaType: type, sessionID: sessionID, error: nil, reason: nil
            ))
        case .connected(let publicToken, let jobID, let site):
            onEvent(PlaidifyLinkEvent(
                source: "plaidify-link",
                event: PlaidifyLinkEventName.connected.rawValue,
                jobID: jobID, publicToken: publicToken,
                organizationID: flow.state.organization?.organizationID,
                organizationName: flow.state.organization?.name,
                site: site,
                mfaType: nil, sessionID: nil, error: nil, reason: nil
            ))
        case .errored(let code, let message):
            onEvent(PlaidifyLinkEvent(
                source: "plaidify-link",
                event: PlaidifyLinkEventName.error.rawValue,
                jobID: nil, publicToken: nil,
                organizationID: flow.state.organization?.organizationID,
                organizationName: flow.state.organization?.name,
                site: flow.state.organization?.site,
                mfaType: nil, sessionID: nil,
                error: "\(code ?? "unknown"): \(message)",
                reason: code
            ))
        case .fallbackToWebView(_, let reason):
            onEvent(PlaidifyLinkEvent(
                source: "plaidify-link",
                event: "FALLBACK_WEBVIEW",
                jobID: nil, publicToken: nil,
                organizationID: flow.state.organization?.organizationID,
                organizationName: flow.state.organization?.name,
                site: flow.state.organization?.site,
                mfaType: nil, sessionID: nil, error: nil, reason: reason
            ))
            presentWebViewFallback()
        }
    }
}
#endif
