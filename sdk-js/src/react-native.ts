import React, { useCallback, useMemo, useState } from "react";

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
  onEvent?: (event: string, payload: PlaidifyLinkEventPayload) => void;
  onSuccess?: (accessToken: string, metadata: PlaidifyLinkSuccessMetadata) => void;
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
  const baseUrl = config.serverUrl.replace(/\/+$/, "");
  const url = new URL(`${baseUrl}/link`);
  url.searchParams.set("token", config.token);

  if (config.origin) {
    url.searchParams.set("origin", config.origin);
  }

  const theme = config.theme;
  if (theme?.accentColor) {
    url.searchParams.set("accent", theme.accentColor);
  }
  if (theme?.bgColor) {
    url.searchParams.set("bg", theme.bgColor);
  }
  if (theme?.borderRadius) {
    url.searchParams.set("radius", theme.borderRadius);
  }
  if (theme?.logo) {
    url.searchParams.set("logo", theme.logo);
  }

  return url.toString();
}

export function createPlaidifyReactNativeWebViewProps(
  config: PlaidifyReactNativeLinkConfig,
): PlaidifyReactNativeWebViewProps {
  return {
    source: { uri: buildPlaidifyHostedLinkUrl(config) },
    originWhitelist: ["*"],
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
  },
) {
  return function handlePlaidifyMessage(input: unknown): PlaidifyLinkEventPayload | null {
    const payload = parsePlaidifyLinkMessage(input);
    if (!payload) {
      return null;
    }

    callbacks?.onLastEventChange?.(payload);
    callbacks?.onEvent?.(String(payload.event || "UNKNOWN"), payload);

    switch (payload.event) {
      case "CONNECTED":
        callbacks?.onStatusChange?.("success");
        callbacks?.onSuccess?.(payload.access_token || "", payload);
        break;
      case "MFA_REQUIRED":
        callbacks?.onStatusChange?.("active");
        callbacks?.onMFA?.({
          mfa_type: payload.mfa_type,
          session_id: payload.session_id,
        });
        break;
      case "ERROR":
        callbacks?.onStatusChange?.("error");
        callbacks?.onExit?.({ reason: "error", error: payload.error });
        break;
      case "EXIT":
      case "DONE":
      case "CLOSE":
        callbacks?.onStatusChange?.("idle");
        callbacks?.onExit?.({ reason: payload.reason || String(payload.event || "exit").toLowerCase() });
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

  const url = useMemo(() => buildPlaidifyHostedLinkUrl(config), [config]);

  const handleMessage = useMemo(
    () =>
      createPlaidifyReactNativeMessageHandler({
        onEvent: config.onEvent,
        onExit: config.onExit,
        onMFA: config.onMFA,
        onSuccess: config.onSuccess,
        onStatusChange: setStatus,
        onLastEventChange: setLastEvent,
      }),
    [config.onEvent, config.onExit, config.onMFA, config.onSuccess],
  );

  const reset = useCallback(() => {
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

  if (!payload || typeof payload !== "object") {
    return null;
  }

  const eventPayload = payload as PlaidifyLinkEventPayload;
  if (eventPayload.source !== "plaidify-link") {
    return null;
  }

  return eventPayload;
}

export function isPlaidifyTerminalEvent(eventName?: string): boolean {
  return ["CONNECTED", "ERROR", "EXIT", "DONE"].includes(String(eventName || ""));
}

export function shouldDismissPlaidifySheet(
  payload: PlaidifyLinkEventPayload | null,
): boolean {
  if (!payload) {
    return false;
  }
  return isPlaidifyTerminalEvent(payload.event);
}