// src/react-native.ts
import React, { useCallback, useMemo, useRef, useState } from "react";

// src/link-events.ts
function sanitizeLinkPayload(data) {
  if (!data || typeof data !== "object") {
    return null;
  }
  const payload = data;
  if (payload.source !== "plaidify-link") {
    return null;
  }
  return {
    source: "plaidify-link",
    event: payload.event,
    error: payload.error,
    error_code: payload.error_code,
    job_id: payload.job_id,
    mfa_type: payload.mfa_type,
    organization_id: payload.organization_id,
    organization_name: payload.organization_name,
    public_token: payload.public_token,
    reason: payload.reason,
    session_id: payload.session_id,
    site: payload.site,
    name: payload.name,
    step: payload.step,
    field: payload.field,
    elapsed_ms: payload.elapsed_ms
  };
}
var TERMINAL_EVENTS = /* @__PURE__ */ new Set(["CONNECTED", "EXIT", "DONE", "CLOSE"]);
function isTerminalLinkEvent(eventName) {
  return TERMINAL_EVENTS.has(String(eventName || ""));
}

// src/link-url.ts
function buildHostedLinkUrl(serverUrl, linkToken, options = {}, base) {
  const url = new URL(`${serverUrl.replace(/\/+$/, "")}/link`, base);
  url.searchParams.set("token", linkToken);
  if (options.origin) {
    url.searchParams.set("origin", options.origin);
  }
  const theme = options.theme;
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

// src/react-native.ts
function buildPlaidifyHostedLinkUrl(config) {
  return buildHostedLinkUrl(config.serverUrl, config.token, {
    origin: config.origin,
    theme: config.theme
  });
}
function plaidifyOrigin(serverUrl) {
  return new URL(serverUrl.replace(/\/+$/, "")).origin;
}
function createPlaidifyReactNativeWebViewProps(config) {
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
    allowsBackForwardNavigationGestures: false
  };
}
function createPlaidifyReactNativeMessageHandler(callbacks) {
  return function handlePlaidifyMessage(input) {
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
          session_id: payload.session_id
        });
        break;
      case "ERROR":
        callbacks?.onStatusChange?.("error");
        break;
      case "EXIT":
      case "DONE":
      case "CLOSE":
        callbacks?.onStatusChange?.("idle");
        callbacks?.onExit?.({
          reason: payload.reason || String(payload.event || "exit").toLowerCase(),
          error: payload.error,
          error_code: payload.error_code
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
function usePlaidifyReactNativeLink(config) {
  const [status, setStatus] = useState("idle");
  const [lastEvent, setLastEvent] = useState(null);
  const finishedRef = useRef(false);
  const url = useMemo(() => buildPlaidifyHostedLinkUrl(config), [config]);
  const { onEvent, onExit, onMFA, onSuccess, serverUrl } = config;
  const handleMessage = useMemo(
    () => createPlaidifyReactNativeMessageHandler({
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
      expectedOrigin: plaidifyOrigin(serverUrl)
    }),
    [onEvent, onExit, onMFA, onSuccess, serverUrl]
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
function messageFromOrigin(input, expectedOrigin) {
  const pageUrl = typeof input === "object" && input !== null && "nativeEvent" in input ? input.nativeEvent?.url : void 0;
  if (typeof pageUrl !== "string" || !pageUrl) {
    return true;
  }
  try {
    return new URL(pageUrl).origin === expectedOrigin;
  } catch {
    return false;
  }
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
  return sanitizeLinkPayload(payload);
}
function isPlaidifyTerminalEvent(eventName) {
  return isTerminalLinkEvent(eventName);
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