package com.plaidify.link

/** Decision returned by [PlaidifyLinkInstitutionRegistry]. */
public sealed class PlaidifyLinkInstitutionStrategy {
    /** The native Compose flow can render this institution. */
    public object Native : PlaidifyLinkInstitutionStrategy()

    /** The institution should be rendered via the WebView fallback. */
    public data class WebViewFallback(val reason: String) : PlaidifyLinkInstitutionStrategy()
}

/**
 * Registry of institutions covered by the native Android UI. Embedders
 * pass a custom registry to opt institutions in; the default registry
 * falls back to webview for everything so a release is required to
 * enable each native connector — release-safe.
 */
public data class PlaidifyLinkInstitutionRegistry(
    val supportedSites: Set<String> = emptySet(),
    val supportedAuthStyles: Set<String> = DEFAULT_SUPPORTED_AUTH_STYLES,
) {
    public fun strategy(organization: PlaidifyOrganization): PlaidifyLinkInstitutionStrategy {
        if (supportedSites.isNotEmpty() && organization.site !in supportedSites) {
            return PlaidifyLinkInstitutionStrategy.WebViewFallback("site_not_in_native_registry")
        }
        val style = organization.authStyle
        if (style != null && style !in supportedAuthStyles) {
            return PlaidifyLinkInstitutionStrategy.WebViewFallback("auth_style_unsupported:$style")
        }
        if (style == null && supportedSites.isEmpty()) {
            return PlaidifyLinkInstitutionStrategy.WebViewFallback("auth_style_unknown")
        }
        return PlaidifyLinkInstitutionStrategy.Native
    }

    public companion object {
        public val DEFAULT_SUPPORTED_AUTH_STYLES: Set<String> = setOf(
            "username_password",
            "username_password_otp",
        )
    }
}
