import React, { useCallback, useMemo, useRef, useState } from "react";

import { isTerminalLinkEvent, sanitizeLinkPayload } from "./link-events";
import { buildHostedLinkUrl, trimTrailingSlashes } from "./link-url";
import type {
  HostedLinkUrlOptions,
  PlaidifyLinkEventPayload,
  PlaidifyLinkExitDetails,
  PlaidifyLinkMfaDetails,
  PlaidifyLinkSuccessMetadata,
} from "./types";

export interface PlaidifyReactNativeLinkConfig {
  serverUrl: string;
  token: string;
  origin?: string;
  theme?: HostedLinkUrlOptions["theme"];
}

export interface PlaidifyReactNativeCallbacks {
  /** Every event, including recoverable ERRORs. */
  onEvent?: (event: string, payload: PlaidifyLinkEventPayload) => void;
  onSuccess?: (publicToken: string, metadata: PlaidifyLinkSuccessMetadata) => void;
  /** The user left Link. Not called for ERROR, which the page recovers from. */
  onExit?: (details: PlaidifyLinkExitDetails) => void;
  onMFA?: (details: PlaidifyLinkMfaDetails) => void;
}

export interface PlaidifyReactNativeHookConfig
  extends PlaidifyReactNativeLinkConfig,
    PlaidifyReactNativeCallbacks {
  webViewProps?: Record<string, unknown>;
}

export interface PlaidifyReactNativeWebViewProps {
  source: { uri: string };
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

export interface UsePlaidifyReactNativeLinkReturn {
  url: string;
  status: "idle" | "active" | "success" | "error";
  lastEvent: PlaidifyLinkEventPayload | null;
  handleMessage: (input: unknown) => PlaidifyLinkEventPayload | null;
  reset: () => void;
  webViewProps: PlaidifyReactNativeWebViewProps;
}

export interface PlaidifyReactNativeLinkComponentProps
  extends PlaidifyReactNativeHookConfig {
  WebViewComponent: React.ComponentType<Record<string, unknown>>;
}

export function buildPlaidifyHostedLinkUrl(
  config: PlaidifyReactNativeLinkConfig,
): string {
  return buildHostedLinkUrl(config.serverUrl, config.token, {
    origin: config.origin,
    theme: config.theme,
  });
}

function plaidifyOrigin(serverUrl: string): string {
  return new URL(trimTrailingSlashes(serverUrl)).origin;
}

export function createPlaidifyReactNativeWebViewProps(
  config: PlaidifyReactNativeLinkConfig,
): PlaidifyReactNativeWebViewProps {
  return {
    source: { uri: buildPlaidifyHostedLinkUrl(config) },
    // Anything off the Plaidify origin is handed to the OS browser instead
    // of being loaded inside the Link webview.
    originWhitelist: [plaidifyOrigin(config.serverUrl)],
    javaScriptEnabled: true,
    domStorageEnabled: true,
    sharedCookiesEnabled: true,
    thirdPartyCookiesEnabled: true,
    startInLoadingState: true,
    allowsBackForwardNavigationGestures: false,
  };
}

export function createPlaidifyReactNativeMessageHandler(
  callbacks?: PlaidifyReactNativeCallbacks & {
    onStatusChange?: (status: UsePlaidifyReactNativeLinkReturn["status"]) => void;
    onLastEventChange?: (payload: PlaidifyLinkEventPayload | null) => void;
    /**
     * Only accept messages from a page on this origin (react-native-webview
     * reports the sending page's URL as `nativeEvent.url`).
     */
    expectedOrigin?: string;
  },
) {
  return function handlePlaidifyMessage(input: unknown): PlaidifyLinkEventPayload | null {
    if (callbacks?.expectedOrigin && !messageFromOrigin(input, callbacks.expectedOrigin)) {
      return null;
    }
    const payload = parsePlaidifyLinkMessage(input);
    if (!payload) {
      return null;
    }

    callbacks?.onLastEventChange?.(payload);
    callbacks?.onEvent?.(String(payload.event || "UNKNOWN"), payload);

    switch (payload.event) {
      case "CONNECTED":
        callbacks?.onStatusChange?.("success");
        callbacks?.onSuccess?.(payload.public_token || "", payload);
        break;
      case "MFA_REQUIRED":
        callbacks?.onStatusChange?.("active");
        callbacks?.onMFA?.({
          mfa_type: payload.mfa_type,
          session_id: payload.session_id,
        });
        break;
      case "ERROR":
        // Recoverable: the page shows retry and choose-another-provider
        // screens, so this is not an exit.
        callbacks?.onStatusChange?.("error");
        break;
      case "EXIT":
      case "DONE":
      case "CLOSE":
        callbacks?.onStatusChange?.("idle");
        callbacks?.onExit?.({
          reason: payload.reason || String(payload.event || "exit").toLowerCase(),
          error: payload.error,
          error_code: payload.error_code,
        });
        break;
      case "TELEMETRY":
        break;
      default:
        callbacks?.onStatusChange?.("active");
        break;
    }

    return payload;
  };
}

export function usePlaidifyReactNativeLink(
  config: PlaidifyReactNativeHookConfig,
): UsePlaidifyReactNativeLinkReturn {
  const [status, setStatus] = useState<UsePlaidifyReactNativeLinkReturn["status"]>("idle");
  const [lastEvent, setLastEvent] = useState<PlaidifyLinkEventPayload | null>(null);
  // onSuccess / onExit fire once per Link session; reset() starts another.
  const finishedRef = useRef(false);

  const url = useMemo(() => buildPlaidifyHostedLinkUrl(config), [config]);

  const { onEvent, onExit, onMFA, onSuccess, serverUrl } = config;
  const handleMessage = useMemo(
    () =>
      createPlaidifyReactNativeMessageHandler({
        onEvent,
        onExit: (details) => {
          if (finishedRef.current) return;
          finishedRef.current = true;
          onExit?.(details);
        },
        onMFA,
        onSuccess: (publicToken, metadata) => {
          if (finishedRef.current) return;
          finishedRef.current = true;
          onSuccess?.(publicToken, metadata);
        },
        onStatusChange: setStatus,
        onLastEventChange: setLastEvent,
        expectedOrigin: plaidifyOrigin(serverUrl),
      }),
    [onEvent, onExit, onMFA, onSuccess, serverUrl],
  );

  const reset = useCallback(() => {
    finishedRef.current = false;
    setStatus("idle");
    setLastEvent(null);
  }, []);

  const webViewProps = useMemo(() => {
    const baseProps = createPlaidifyReactNativeWebViewProps(config);
    const externalOnMessage = config.webViewProps?.onMessage;

    return {
      ...baseProps,
      ...config.webViewProps,
      source: { uri: url },
      onMessage: (event: unknown) => {
        handleMessage(event);
        if (typeof externalOnMessage === "function") {
          externalOnMessage(event);
        }
      },
    } as PlaidifyReactNativeWebViewProps;
  }, [config, handleMessage, url]);

  return {
    url,
    status,
    lastEvent,
    handleMessage,
    reset,
    webViewProps,
  };
}

export function PlaidifyReactNativeLink(
  props: PlaidifyReactNativeLinkComponentProps,
) {
  const { WebViewComponent, ...config } = props;
  const { webViewProps } = usePlaidifyReactNativeLink(config);
  return React.createElement(WebViewComponent, webViewProps as Record<string, unknown>);
}

/** True unless the message says it came from a page on another origin. */
function messageFromOrigin(input: unknown, expectedOrigin: string): boolean {
  const pageUrl =
    typeof input === "object" && input !== null && "nativeEvent" in input
      ? (input as { nativeEvent?: { url?: unknown } }).nativeEvent?.url
      : undefined;
  if (typeof pageUrl !== "string" || !pageUrl) {
    return true;
  }
  try {
    return new URL(pageUrl).origin === expectedOrigin;
  } catch {
    return false;
  }
}

export function parsePlaidifyLinkMessage(
  input: unknown,
): PlaidifyLinkEventPayload | null {
  let payload: unknown = input;

  if (
    typeof payload === "object" &&
    payload !== null &&
    "nativeEvent" in payload &&
    typeof (payload as { nativeEvent?: { data?: unknown } }).nativeEvent?.data !==
      "undefined"
  ) {
    payload = (payload as { nativeEvent?: { data?: unknown } }).nativeEvent?.data;
  }

  if (typeof payload === "string") {
    try {
      payload = JSON.parse(payload);
    } catch {
      return null;
    }
  }

  return sanitizeLinkPayload(payload);
}

/** CONNECTED or an exit — never ERROR, which the page recovers from. */
export function isPlaidifyTerminalEvent(eventName?: string): boolean {
  return isTerminalLinkEvent(eventName);
}

export function shouldDismissPlaidifySheet(
  payload: PlaidifyLinkEventPayload | null,
): boolean {
  if (!payload) {
    return false;
  }
  return isPlaidifyTerminalEvent(payload.event);
}
