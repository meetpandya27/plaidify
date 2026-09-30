package com.plaidify.link

/** Decision returned by [PlaidifyLinkInstitutionRegistry]. */
public sealed class PlaidifyLinkInstitutionStrategy {
    /** The native Compose flow can render this institution. */
    public object Native : PlaidifyLinkInstitutionStrategy()

    /** The institution should be rendered via the WebView fallback. */
    public data class WebViewFallback(val reason: String) : PlaidifyLinkInstitutionStrategy()
}

/**
 * Registry of institutions covered by the (experimental) native Android UI.
 * Embedders pass a custom registry to opt institutions in; the default
 * registry lists no sites, so everything uses the hosted WebView until a
 * site is explicitly listed and verified.
 */
public data class PlaidifyLinkInstitutionRegistry(
    val supportedSites: Set<String> = emptySet(),
    val supportedAuthStyles: Set<String> = DEFAULT_SUPPORTED_AUTH_STYLES,
) {
    public fun strategy(organization: PlaidifyOrganization): PlaidifyLinkInstitutionStrategy {
        // An empty list covers nothing: native screens are opt-in per site.
        if (organization.site !in supportedSites) {
            return PlaidifyLinkInstitutionStrategy.WebViewFallback("site_not_in_native_registry")
        }
        val style = organization.authStyle
            ?: return PlaidifyLinkInstitutionStrategy.WebViewFallback("auth_style_unknown")
        if (style !in supportedAuthStyles) {
            return PlaidifyLinkInstitutionStrategy.WebViewFallback("auth_style_unsupported:$style")
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
