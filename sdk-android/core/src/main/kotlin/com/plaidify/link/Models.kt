package com.plaidify.link

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Errors emitted by [PlaidifyLinkClient]. Mirrors the Swift counterpart. */
public sealed class PlaidifyLinkClientException(message: String) : RuntimeException(message) {
    public class InvalidUrl(url: String) : PlaidifyLinkClientException("Invalid URL: $url")
    public class Transport(message: String) : PlaidifyLinkClientException(message)
    public class Http(
        public val status: Int,
        public val errorCode: String?,
        message: String,
    ) : PlaidifyLinkClientException(message)

    public class Decoding(message: String) : PlaidifyLinkClientException(message)
}

/** Status payload returned by `GET /link/sessions/{token}/status`. */
@Serializable
public data class PlaidifyLinkSessionStatus(
    val status: String,
    val site: String? = null,
    @SerialName("mfa_type") val mfaType: String? = null,
    @SerialName("session_id") val sessionId: String? = null,
    @SerialName("public_token") val publicToken: String? = null,
    @SerialName("error_message") val errorMessage: String? = null,
)

/** Organization record returned by `/organizations/search`. */
@Serializable
public data class PlaidifyOrganization(
    @SerialName("organization_id") val organizationId: String,
    val name: String,
    val site: String,
    @SerialName("logo_url") val logoUrl: String? = null,
    @SerialName("primary_color") val primaryColor: String? = null,
    @SerialName("accent_color") val accentColor: String? = null,
    @SerialName("secondary_color") val secondaryColor: String? = null,
    @SerialName("hint_copy") val hintCopy: String? = null,
    @SerialName("auth_style") val authStyle: String? = null,
)

@Serializable
public data class PlaidifyOrganizationSearchResponse(
    val organizations: List<PlaidifyOrganization>,
)

/** Encrypted credential pair posted to `/connect`. */
public data class PlaidifyEncryptedCredentials(
    val username: String,
    val password: String,
)

/** Response payload from `/connect` and `/mfa/submit`. */
@Serializable
public data class PlaidifyConnectResponse(
    val status: String,
    @SerialName("session_id") val sessionId: String? = null,
    @SerialName("mfa_type") val mfaType: String? = null,
    @SerialName("public_token") val publicToken: String? = null,
    @SerialName("job_id") val jobId: String? = null,
    val message: String? = null,
    @SerialName("error_message") val errorMessage: String? = null,
)
