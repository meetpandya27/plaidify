import React from 'react';

interface HostedLinkUrlOptions {
    origin?: string;
    theme?: LinkTheme;
}
type PlaidifyLinkEventName = "OPEN" | "CLOSE" | "INSTITUTION_SELECTED" | "CREDENTIALS_SUBMITTED" | "MFA_REQUIRED" | "MFA_SUBMITTED" | "CONNECTED" | "ERROR" | "EXIT" | "DONE";
interface PlaidifyLinkMfaDetails {
    mfa_type?: string;
    session_id?: string;
}
interface PlaidifyLinkExitDetails {
    reason?: string;
    error?: string;
}
interface PlaidifyLinkSuccessMetadata {
    data?: Record<string, unknown>;
    organization_id?: string;
    organization_name?: string;
    public_token?: string;
    site?: string;
}
interface PlaidifyLinkEventPayload extends PlaidifyLinkExitDetails, PlaidifyLinkMfaDetails {
    source?: "plaidify-link";
    event?: PlaidifyLinkEventName | string;
    access_token?: string;
    public_token?: string;
    organization_id?: string;
    organization_name?: string;
    site?: string;
    data?: Record<string, unknown>;
}
interface LinkTheme {
    accentColor?: string;
    bgColor?: string;
    borderRadius?: string;
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
    onEvent?: (event: string, payload: PlaidifyLinkEventPayload) => void;
    onSuccess?: (accessToken: string, metadata: PlaidifyLinkSuccessMetadata) => void;
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
}): (input: unknown) => PlaidifyLinkEventPayload | null;
declare function usePlaidifyReactNativeLink(config: PlaidifyReactNativeHookConfig): UsePlaidifyReactNativeLinkReturn;
declare function PlaidifyReactNativeLink(props: PlaidifyReactNativeLinkComponentProps): React.ReactElement<Record<string, unknown>, string | React.JSXElementConstructor<any>>;
declare function parsePlaidifyLinkMessage(input: unknown): PlaidifyLinkEventPayload | null;
declare function isPlaidifyTerminalEvent(eventName?: string): boolean;
declare function shouldDismissPlaidifySheet(payload: PlaidifyLinkEventPayload | null): boolean;

export { type PlaidifyReactNativeCallbacks, type PlaidifyReactNativeHookConfig, PlaidifyReactNativeLink, type PlaidifyReactNativeLinkComponentProps, type PlaidifyReactNativeLinkConfig, type PlaidifyReactNativeWebViewProps, type UsePlaidifyReactNativeLinkReturn, buildPlaidifyHostedLinkUrl, createPlaidifyReactNativeMessageHandler, createPlaidifyReactNativeWebViewProps, isPlaidifyTerminalEvent, parsePlaidifyLinkMessage, shouldDismissPlaidifySheet, usePlaidifyReactNativeLink };
