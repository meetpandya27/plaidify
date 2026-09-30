import Foundation
import WebKit
#if canImport(UIKit)
import UIKit
#elseif canImport(AppKit)
import AppKit
#endif

public struct PlaidifyLinkTheme: Equatable {
    /// Buttons and focus rings, as a hex colour ("#0b8f73").
    public var accentColor: String?
    /// Page background behind the Link card, as a hex colour.
    public var backgroundColor: String?
    /// Corner radius of the Link card, e.g. "24px".
    public var borderRadius: String?
    /// Logo above every step, as a `data:image/…;base64,` URI (at most
    /// 32 KB). The hosted page only loads images from itself and data: URIs.
    public var logo: String?

    public init(
        accentColor: String? = nil,
        backgroundColor: String? = nil,
        borderRadius: String? = nil,
        logo: String? = nil
    ) {
        self.accentColor = accentColor
        self.backgroundColor = backgroundColor
        self.borderRadius = borderRadius
        self.logo = logo
    }
}

public struct PlaidifyHostedLinkConfiguration: Equatable {
    public var serverURL: URL
    public var token: String
    public var origin: String?
    public var theme: PlaidifyLinkTheme

    public init(
        serverURL: URL,
        token: String,
        origin: String? = nil,
        theme: PlaidifyLinkTheme = PlaidifyLinkTheme()
    ) {
        self.serverURL = serverURL
        self.token = token
        self.origin = origin
        self.theme = theme
    }

    public func hostedLinkURL() -> URL {
        let normalizedBase = serverURL.absoluteString.hasSuffix("/")
            ? String(serverURL.absoluteString.dropLast())
            : serverURL.absoluteString
        var components = URLComponents(string: normalizedBase + "/link") ?? URLComponents()
        var queryItems = [
            URLQueryItem(name: "token", value: token),
        ]

        if let origin {
            queryItems.append(URLQueryItem(name: "origin", value: origin))
        }
        if let accentColor = theme.accentColor {
            queryItems.append(URLQueryItem(name: "accent", value: accentColor))
        }
        if let backgroundColor = theme.backgroundColor {
            queryItems.append(URLQueryItem(name: "bg", value: backgroundColor))
        }
        if let borderRadius = theme.borderRadius {
            queryItems.append(URLQueryItem(name: "radius", value: borderRadius))
        }
        if let logo = theme.logo {
            queryItems.append(URLQueryItem(name: "logo", value: logo))
        }

        components.queryItems = queryItems
        // `+` is a space to the page's URLSearchParams; data: URIs are full of them.
        components.percentEncodedQuery = components.percentEncodedQuery?
            .replacingOccurrences(of: "+", with: "%2B")
        return components.url ?? serverURL
    }

    public func urlRequest(cachePolicy: URLRequest.CachePolicy = .reloadIgnoringLocalCacheData) -> URLRequest {
        URLRequest(url: hostedLinkURL(), cachePolicy: cachePolicy)
    }

    /// The only origin the Link web view may show and hear from.
    public var plaidifyOrigin: PlaidifyLinkOrigin? {
        PlaidifyLinkOrigin(url: serverURL)
    }
}

public enum PlaidifyLinkEventName: String, Codable, CaseIterable {
    case open = "OPEN"
    case close = "CLOSE"
    case institutionSelected = "INSTITUTION_SELECTED"
    case credentialsSubmitted = "CREDENTIALS_SUBMITTED"
    case mfaRequired = "MFA_REQUIRED"
    case mfaSubmitted = "MFA_SUBMITTED"
    case connected = "CONNECTED"
    case error = "ERROR"
    case exit = "EXIT"
    case done = "DONE"
}

public struct PlaidifyLinkEvent: Codable, Equatable {
    public let source: String
    public let event: String
    public let jobID: String?
    public let publicToken: String?
    public let organizationID: String?
    public let organizationName: String?
    public let site: String?
    public let mfaType: String?
    public let sessionID: String?
    public let error: String?
    public let reason: String?
    /// Error-taxonomy code on ERROR, and on an EXIT taken from an error screen.
    public let errorCode: String?

    enum CodingKeys: String, CodingKey {
        case source
        case event
        case jobID = "job_id"
        case publicToken = "public_token"
        case organizationID = "organization_id"
        case organizationName = "organization_name"
        case site
        case mfaType = "mfa_type"
        case sessionID = "session_id"
        case error
        case reason
        case errorCode = "error_code"
    }

    public init(
        source: String = "plaidify-link",
        event: String,
        jobID: String? = nil,
        publicToken: String? = nil,
        organizationID: String? = nil,
        organizationName: String? = nil,
        site: String? = nil,
        mfaType: String? = nil,
        sessionID: String? = nil,
        error: String? = nil,
        reason: String? = nil,
        errorCode: String? = nil
    ) {
        self.source = source
        self.event = event
        self.jobID = jobID
        self.publicToken = publicToken
        self.organizationID = organizationID
        self.organizationName = organizationName
        self.site = site
        self.mfaType = mfaType
        self.sessionID = sessionID
        self.error = error
        self.reason = reason
        self.errorCode = errorCode
    }

    public var name: PlaidifyLinkEventName? {
        PlaidifyLinkEventName(rawValue: event.uppercased())
    }

    /// Link is finished: connected, or the user left. ERROR is not terminal —
    /// the page shows retry and choose-another-provider screens after it.
    public var isTerminal: Bool {
        guard let name else {
            return false
        }

        switch name {
        case .connected, .exit, .done, .close:
            return true
        default:
            return false
        }
    }

    public var shouldDismissSheet: Bool {
        isTerminal
    }
}

public enum PlaidifyLinkMessageParser {
    public static func parse(data: Data) -> PlaidifyLinkEvent? {
        let decoder = JSONDecoder()

        guard let payload = try? decoder.decode(PlaidifyLinkEvent.self, from: data),
              payload.source == "plaidify-link" else {
            return nil
        }

        return payload
    }

    public static func parse(string: String) -> PlaidifyLinkEvent? {
        guard let data = string.data(using: .utf8) else {
            return nil
        }

        return parse(data: data)
    }

    public static func parse(body: Any) -> PlaidifyLinkEvent? {
        if let string = body as? String {
            return parse(string: string)
        }

        guard JSONSerialization.isValidJSONObject(body),
              let data = try? JSONSerialization.data(withJSONObject: body),
              let payload = parse(data: data) else {
            return nil
        }

        return payload
    }
}

// MARK: - Origin and navigation policy

/// A web origin (scheme, host, port) compared the way browsers do.
public struct PlaidifyLinkOrigin: Equatable {
    public let scheme: String
    public let host: String
    /// The effective port; default ports are made explicit.
    public let port: Int

    public init?(url: URL) {
        guard let scheme = url.scheme?.lowercased(), let host = url.host?.lowercased(), !host.isEmpty else {
            return nil
        }
        self.init(scheme: scheme, host: host, port: url.port ?? 0)
    }

    /// `port` 0 means the scheme's default port (what WKSecurityOrigin reports).
    public init(scheme: String, host: String, port: Int) {
        let scheme = scheme.lowercased()
        self.scheme = scheme
        self.host = host.lowercased()
        if port != 0 {
            self.port = port
        } else {
            self.port = scheme == "https" ? 443 : (scheme == "http" ? 80 : 0)
        }
    }

    public func matches(_ url: URL?) -> Bool {
        guard let url, let other = PlaidifyLinkOrigin(url: url) else {
            return false
        }
        return other == self
    }

    /// Whether a bridge message may be trusted: only from the Plaidify
    /// origin's main frame, never from an iframe or a page navigated to.
    public func acceptsMessage(isMainFrame: Bool, scheme: String, host: String, port: Int) -> Bool {
        isMainFrame && PlaidifyLinkOrigin(scheme: scheme, host: host, port: port) == self
    }
}

public enum PlaidifyLinkNavigationDecision: Equatable {
    case allow
    case cancel
    /// Leave the Link web view and hand the URL to the system browser.
    case openExternally(URL)
}

public enum PlaidifyLinkNavigationPolicy {
    /// Everything but the Plaidify origin stays out of the Link web view:
    /// links the user follows open in the browser; anything else is dropped.
    public static func decide(
        url: URL?,
        isMainFrame: Bool,
        allowedOrigin: PlaidifyLinkOrigin
    ) -> PlaidifyLinkNavigationDecision {
        guard let url else {
            return .cancel
        }
        if allowedOrigin.matches(url) {
            return .allow
        }
        let scheme = url.scheme?.lowercased() ?? ""
        if isMainFrame, ["http", "https", "mailto", "tel"].contains(scheme) {
            return .openExternally(url)
        }
        return .cancel
    }
}

// MARK: - WebKit bridge

public final class PlaidifyLinkScriptMessageHandler: NSObject, WKScriptMessageHandler {
    public static let bridgeName = "plaidifyLink"

    private let allowedOrigin: PlaidifyLinkOrigin
    private let onEvent: (PlaidifyLinkEvent) -> Void

    /// - Parameters:
    ///   - allowedOrigin: The Plaidify server's origin; messages from any
    ///     other origin, or from a subframe, are ignored.
    ///   - onEvent: Called on the main thread for each trusted event.
    public init(allowedOrigin: PlaidifyLinkOrigin, onEvent: @escaping (PlaidifyLinkEvent) -> Void) {
        self.allowedOrigin = allowedOrigin
        self.onEvent = onEvent
    }

    public func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        let origin = message.frameInfo.securityOrigin
        guard message.name == Self.bridgeName,
              allowedOrigin.acceptsMessage(
                  isMainFrame: message.frameInfo.isMainFrame,
                  scheme: origin.protocol,
                  host: origin.host,
                  port: origin.port
              ),
              let payload = PlaidifyLinkMessageParser.parse(body: message.body) else {
            return
        }

        if Thread.isMainThread {
            onEvent(payload)
        } else {
            DispatchQueue.main.async { [onEvent] in onEvent(payload) }
        }
    }
}

/// WKUserContentController retains its handlers; this proxy keeps it from
/// retaining (and leaking) whatever the real handler's closure captures.
private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?

    init(_ target: WKScriptMessageHandler) {
        self.target = target
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(userContentController, didReceive: message)
    }
}

/// Keeps the Link web view on the Plaidify origin.
public final class PlaidifyLinkNavigationDelegate: NSObject, WKNavigationDelegate, WKUIDelegate {
    private let allowedOrigin: PlaidifyLinkOrigin
    private let openExternally: (URL) -> Void

    public init(allowedOrigin: PlaidifyLinkOrigin, openExternally: @escaping (URL) -> Void) {
        self.allowedOrigin = allowedOrigin
        self.openExternally = openExternally
    }

    public func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        let isMainFrame = navigationAction.targetFrame?.isMainFrame ?? true
        switch PlaidifyLinkNavigationPolicy.decide(
            url: navigationAction.request.url,
            isMainFrame: isMainFrame,
            allowedOrigin: allowedOrigin
        ) {
        case .allow:
            decisionHandler(.allow)
        case .cancel:
            decisionHandler(.cancel)
        case .openExternally(let url):
            decisionHandler(.cancel)
            openExternally(url)
        }
    }

    /// `target="_blank"` / `window.open`: never a second web view.
    public func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if case .openExternally(let url) = PlaidifyLinkNavigationPolicy.decide(
            url: navigationAction.request.url,
            isMainFrame: true,
            allowedOrigin: allowedOrigin
        ) {
            openExternally(url)
        }
        return nil
    }
}

/// The hosted Link page in a WKWebView, with the bridge and navigation
/// locked to the Plaidify origin. Keep a reference while it is on screen.
public final class PlaidifyHostedLinkWebView {
    public let webView: WKWebView
    public let configuration: PlaidifyHostedLinkConfiguration

    private let messageHandler: PlaidifyLinkScriptMessageHandler
    private let navigationDelegate: PlaidifyLinkNavigationDelegate

    /// Returns nil when `configuration.serverURL` has no usable origin.
    public init?(
        configuration: PlaidifyHostedLinkConfiguration,
        openExternally: @escaping (URL) -> Void = PlaidifyHostedLinkWebView.openInSystemBrowser,
        onEvent: @escaping (PlaidifyLinkEvent) -> Void
    ) {
        guard let origin = configuration.plaidifyOrigin else {
            return nil
        }
        self.configuration = configuration
        self.messageHandler = PlaidifyLinkScriptMessageHandler(allowedOrigin: origin, onEvent: onEvent)
        self.navigationDelegate = PlaidifyLinkNavigationDelegate(allowedOrigin: origin, openExternally: openExternally)
        self.webView = WKWebView(
            frame: .zero,
            configuration: PlaidifyLinkWebViewFactory.makeConfiguration(messageHandler: messageHandler)
        )
        webView.navigationDelegate = navigationDelegate
        webView.uiDelegate = navigationDelegate
    }

    deinit {
        webView.configuration.userContentController
            .removeScriptMessageHandler(forName: PlaidifyLinkScriptMessageHandler.bridgeName)
    }

    public func load() {
        webView.load(configuration.urlRequest())
    }

    public static func openInSystemBrowser(_ url: URL) {
        #if canImport(UIKit) && !os(watchOS)
        UIApplication.shared.open(url)
        #elseif canImport(AppKit)
        NSWorkspace.shared.open(url)
        #endif
    }
}

public enum PlaidifyLinkWebViewFactory {
    /// A configuration whose `plaidifyLink` bridge delivers to `messageHandler`.
    public static func makeConfiguration(messageHandler: WKScriptMessageHandler) -> WKWebViewConfiguration {
        let contentController = WKUserContentController()
        contentController.add(
            WeakScriptMessageHandler(messageHandler),
            name: PlaidifyLinkScriptMessageHandler.bridgeName
        )

        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.userContentController = contentController
        return configuration
    }

    /// The hosted Link page, loading, locked to the Plaidify origin.
    public static func makeHostedLinkWebView(
        hostedLink configuration: PlaidifyHostedLinkConfiguration,
        onEvent: @escaping (PlaidifyLinkEvent) -> Void
    ) -> PlaidifyHostedLinkWebView? {
        let hosted = PlaidifyHostedLinkWebView(configuration: configuration, onEvent: onEvent)
        hosted?.load()
        return hosted
    }
}
