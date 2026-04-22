import Foundation
import WebKit

public struct PlaidifyLinkTheme: Equatable {
    public var accentColor: String?
    public var backgroundColor: String?
    public var borderRadius: String?
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
        return components.url ?? serverURL
    }

    public func urlRequest(cachePolicy: URLRequest.CachePolicy = .reloadIgnoringLocalCacheData) -> URLRequest {
        URLRequest(url: hostedLinkURL(), cachePolicy: cachePolicy)
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
    }

    public var name: PlaidifyLinkEventName? {
        PlaidifyLinkEventName(rawValue: event.uppercased())
    }

    public var isTerminal: Bool {
        guard let name else {
            return false
        }

        switch name {
        case .connected, .error, .exit, .done:
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

public final class PlaidifyLinkScriptMessageHandler: NSObject, WKScriptMessageHandler {
    public static let bridgeName = "plaidifyLink"

    private let onEvent: (PlaidifyLinkEvent) -> Void

    public init(onEvent: @escaping (PlaidifyLinkEvent) -> Void) {
        self.onEvent = onEvent
    }

    public func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == Self.bridgeName,
              let payload = PlaidifyLinkMessageParser.parse(body: message.body) else {
            return
        }

        onEvent(payload)
    }
}

public enum PlaidifyLinkWebViewFactory {
    public static func makeConfiguration(messageHandler: WKScriptMessageHandler) -> WKWebViewConfiguration {
        let contentController = WKUserContentController()
        contentController.add(messageHandler, name: PlaidifyLinkScriptMessageHandler.bridgeName)

        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.userContentController = contentController
        return configuration
    }

    public static func makeWebView(
        hostedLink configuration: PlaidifyHostedLinkConfiguration,
        messageHandler: WKScriptMessageHandler
    ) -> WKWebView {
        let webView = WKWebView(frame: .zero, configuration: makeConfiguration(messageHandler: messageHandler))
        webView.load(configuration.urlRequest())
        return webView
    }
}