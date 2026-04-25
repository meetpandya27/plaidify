import Foundation

/// Decision returned by the registry when the SDK asks "can I render
/// the native flow for this institution?"
public enum PlaidifyLinkInstitutionStrategy: Equatable {
    /// The native picker / credentials / MFA screens should be used.
    case native
    /// The institution requires the WKWebView hosted-link fallback.
    case webViewFallback(reason: String)
}

/// Registry of institutions covered by the native UI. Long-tail
/// institutions fall back to the WKWebView so they continue to work
/// without a UI release.
public struct PlaidifyLinkInstitutionRegistry: Equatable {
    public let supportedSites: Set<String>
    public let supportedAuthStyles: Set<String>

    public init(
        supportedSites: Set<String> = PlaidifyLinkInstitutionRegistry.defaultSupportedSites,
        supportedAuthStyles: Set<String> = PlaidifyLinkInstitutionRegistry.defaultSupportedAuthStyles
    ) {
        self.supportedSites = supportedSites
        self.supportedAuthStyles = supportedAuthStyles
    }

    /// Default coverage. Conservative: only username/password style
    /// institutions are eligible for native rendering today; everything
    /// else (OAuth redirects, security questions, push) falls through.
    public static let defaultSupportedAuthStyles: Set<String> = [
        "username_password",
        "username_password_otp",
    ]

    /// Out-of-the-box, the native flow covers no specific sites; embedders
    /// may opt institutions in by passing a custom registry. This keeps
    /// a release-safe default of webview for everything until each
    /// connector is explicitly verified.
    public static let defaultSupportedSites: Set<String> = []

    /// Decide whether ``organization`` can be rendered natively.
    public func strategy(for organization: PlaidifyOrganization) -> PlaidifyLinkInstitutionStrategy {
        if !supportedSites.isEmpty && !supportedSites.contains(organization.site) {
            return .webViewFallback(reason: "site_not_in_native_registry")
        }
        if let style = organization.authStyle, !supportedAuthStyles.contains(style) {
            return .webViewFallback(reason: "auth_style_unsupported:\(style)")
        }
        if organization.authStyle == nil && supportedSites.isEmpty {
            // Empty registry + unknown auth style → webview is the safer choice.
            return .webViewFallback(reason: "auth_style_unknown")
        }
        return .native
    }
}
