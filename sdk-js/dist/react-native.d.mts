import React from 'react';

interface HostedLinkUrlOptions {
    /** Origin of the page embedding Link (web embeds only). */
    origin?: string;
    theme?: LinkTheme;
}
type PlaidifyLinkEventName = "OPEN" | "CLOSE" | "INSTITUTION_SELECTED" | "CREDENTIALS_SUBMITTED" | "MFA_REQUIRED" | "MFA_SUBMITTED" | "CONNECTED" | "ERROR" | "EXIT" | "DONE" | "TELEMETRY" | "SUPPORT_REQUESTED";
interface PlaidifyLinkMfaDetails {
    mfa_type?: string;
    session_id?: string;
}
interface PlaidifyLinkExitDetails {
    reason?: string;
    error?: string;
    /** Error-taxonomy code of the last error, when the user exits from one. */
    error_code?: string;
}
interface PlaidifyLinkSuccessMetadata {
    job_id?: string;
    organization_id?: string;
    organization_name?: string;
    public_token?: string;
    site?: string;
}
interface PlaidifyLinkEventPayload extends PlaidifyLinkExitDetails, PlaidifyLinkMfaDetails {
    source?: "plaidify-link";
    event?: PlaidifyLinkEventName | string;
    job_id?: string;
    public_token?: string;
    organization_id?: string;
    organization_name?: string;
    site?: string;
    /** TELEMETRY only: which telemetry event (step_view, field_error, …). */
    name?: string;
    /** TELEMETRY only: the step the event concerns. */
    step?: string;
    /** TELEMETRY only: the form field that failed validation (never its value). */
    field?: string;
    /** TELEMETRY only: milliseconds since Link opened. */
    elapsed_ms?: number;
}
interface LinkTheme {
    /** Buttons and focus rings, as a hex colour ("#0b8f73"). */
    accentColor?: string;
    /** Page background behind the Link card, as a hex colour. */
    bgColor?: string;
    /** Corner radius of the Link card, e.g. "24px" or "1.5rem". */
    borderRadius?: string;
    /**
     * Logo shown above every step, as a `data:image/…;base64,` URI of at
     * most 32 KB. The hosted page only loads images from itself and data:
     * URIs, so remote URLs are ignored.
     */
    logo?: string;
    fullscreenOnMobile?: boolean;
    mobileBreakpoint?: number;
}

interface PlaidifyReactNativeLinkConfig {
    serverUrl: string;
    token: string;
    origin?: string;
    theme?: HostedLinkUrlOptions["theme"];
}
interface PlaidifyReactNativeCallbacks {
    /** Every event, including recoverable ERRORs. */
    onEvent?: (event: string, payload: PlaidifyLinkEventPayload) => void;
    onSuccess?: (publicToken: string, metadata: PlaidifyLinkSuccessMetadata) => void;
    /** The user left Link. Not called for ERROR, which the page recovers from. */
    onExit?: (details: PlaidifyLinkExitDetails) => void;
    onMFA?: (details: PlaidifyLinkMfaDetails) => void;
}
interface PlaidifyReactNativeHookConfig extends PlaidifyReactNativeLinkConfig, PlaidifyReactNativeCallbacks {
    webViewProps?: Record<string, unknown>;
}
interface PlaidifyReactNativeWebViewProps {
    source: {
        uri: string;
    };
    originWhitelist: string[];
    javaScriptEnabled: boolean;
    domStorageEnabled: boolean;
    sharedCookiesEnabled: boolean;
    thirdPartyCookiesEnabled: boolean;
    startInLoadingState: boolean;
    allowsBackForwardNavigationGestures: boolean;
    onMessage?: (event: unknown) => void;
    [key: string]: unknown;
}
interface UsePlaidifyReactNativeLinkReturn {
    url: string;
    status: "idle" | "active" | "success" | "error";
    lastEvent: PlaidifyLinkEventPayload | null;
    handleMessage: (input: unknown) => PlaidifyLinkEventPayload | null;
    reset: () => void;
    webViewProps: PlaidifyReactNativeWebViewProps;
}
interface PlaidifyReactNativeLinkComponentProps extends PlaidifyReactNativeHookConfig {
    WebViewComponent: React.ComponentType<Record<string, unknown>>;
}
declare function buildPlaidifyHostedLinkUrl(config: PlaidifyReactNativeLinkConfig): string;
declare function createPlaidifyReactNativeWebViewProps(config: PlaidifyReactNativeLinkConfig): PlaidifyReactNativeWebViewProps;
declare function createPlaidifyReactNativeMessageHandler(callbacks?: PlaidifyReactNativeCallbacks & {
    onStatusChange?: (status: UsePlaidifyReactNativeLinkReturn["status"]) => void;
    onLastEventChange?: (payload: PlaidifyLinkEventPayload | null) => void;
    /**
     * Only accept messages from a page on this origin (react-native-webview
     * reports the sending page's URL as `nativeEvent.url`).
     */
    expectedOrigin?: string;
}): (input: unknown) => PlaidifyLinkEventPayload | null;
declare function usePlaidifyReactNativeLink(config: PlaidifyReactNativeHookConfig): UsePlaidifyReactNativeLinkReturn;
declare function PlaidifyReactNativeLink(props: PlaidifyReactNativeLinkComponentProps): React.ReactElement<Record<string, unknown>, string | React.JSXElementConstructor<any>>;
declare function parsePlaidifyLinkMessage(input: unknown): PlaidifyLinkEventPayload | null;
/** CONNECTED or an exit — never ERROR, which the page recovers from. */
declare function isPlaidifyTerminalEvent(eventName?: string): boolean;
declare function shouldDismissPlaidifySheet(payload: PlaidifyLinkEventPayload | null): boolean;

export { type PlaidifyReactNativeCallbacks, type PlaidifyReactNativeHookConfig, PlaidifyReactNativeLink, type PlaidifyReactNativeLinkComponentProps, type PlaidifyReactNativeLinkConfig, type PlaidifyReactNativeWebViewProps, type UsePlaidifyReactNativeLinkReturn, buildPlaidifyHostedLinkUrl, createPlaidifyReactNativeMessageHandler, createPlaidifyReactNativeWebViewProps, isPlaidifyTerminalEvent, parsePlaidifyLinkMessage, shouldDismissPlaidifySheet, usePlaidifyReactNativeLink };
