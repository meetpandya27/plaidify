// src/react-native.ts
import React, { useCallback, useMemo, useState } from "react";
function buildPlaidifyHostedLinkUrl(config) {
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
function createPlaidifyReactNativeWebViewProps(config) {
  const origin = new URL(config.serverUrl.replace(/\/+$/, "")).origin;
  return {
    source: { uri: buildPlaidifyHostedLinkUrl(config) },
    originWhitelist: [origin],
    javaScriptEnabled: true,
    domStorageEnabled: true,
    sharedCookiesEnabled: true,
    thirdPartyCookiesEnabled: true,
    startInLoadingState: true,
    allowsBackForwardNavigationGestures: false
  };
}
function createPlaidifyReactNativeMessageHandler(callbacks) {
  return function handlePlaidifyMessage(input) {
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
          session_id: payload.session_id
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
function usePlaidifyReactNativeLink(config) {
  const [status, setStatus] = useState("idle");
  const [lastEvent, setLastEvent] = useState(null);
  const url = useMemo(() => buildPlaidifyHostedLinkUrl(config), [config]);
  const handleMessage = useMemo(
    () => createPlaidifyReactNativeMessageHandler({
      onEvent: config.onEvent,
      onExit: config.onExit,
      onMFA: config.onMFA,
      onSuccess: config.onSuccess,
      onStatusChange: setStatus,
      onLastEventChange: setLastEvent
    }),
    [config.onEvent, config.onExit, config.onMFA, config.onSuccess]
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
      onMessage: (event) => {
        handleMessage(event);
        if (typeof externalOnMessage === "function") {
          externalOnMessage(event);
        }
      }
    };
  }, [config, handleMessage, url]);
  return {
    url,
    status,
    lastEvent,
    handleMessage,
    reset,
    webViewProps
  };
}
function PlaidifyReactNativeLink(props) {
  const { WebViewComponent, ...config } = props;
  const { webViewProps } = usePlaidifyReactNativeLink(config);
  return React.createElement(WebViewComponent, webViewProps);
}
function parsePlaidifyLinkMessage(input) {
  let payload = input;
  if (typeof payload === "object" && payload !== null && "nativeEvent" in payload && typeof payload.nativeEvent?.data !== "undefined") {
    payload = payload.nativeEvent?.data;
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
  const eventPayload = payload;
  if (eventPayload.source !== "plaidify-link") {
    return null;
  }
  return {
    source: "plaidify-link",
    event: eventPayload.event,
    error: eventPayload.error,
    job_id: eventPayload.job_id,
    mfa_type: eventPayload.mfa_type,
    organization_id: eventPayload.organization_id,
    organization_name: eventPayload.organization_name,
    public_token: eventPayload.public_token,
    reason: eventPayload.reason,
    session_id: eventPayload.session_id,
    site: eventPayload.site
  };
}
function isPlaidifyTerminalEvent(eventName) {
  return ["CONNECTED", "ERROR", "EXIT", "DONE"].includes(String(eventName || ""));
}
function shouldDismissPlaidifySheet(payload) {
  if (!payload) {
    return false;
  }
  return isPlaidifyTerminalEvent(payload.event);
}
export {
  PlaidifyReactNativeLink,
  buildPlaidifyHostedLinkUrl,
  createPlaidifyReactNativeMessageHandler,
  createPlaidifyReactNativeWebViewProps,
  isPlaidifyTerminalEvent,
  parsePlaidifyLinkMessage,
  shouldDismissPlaidifySheet,
  usePlaidifyReactNativeLink
};
//# sourceMappingURL=react-native.mjs.map