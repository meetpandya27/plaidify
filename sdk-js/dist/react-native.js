"use strict";
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// src/react-native.ts
var react_native_exports = {};
__export(react_native_exports, {
  PlaidifyReactNativeLink: () => PlaidifyReactNativeLink,
  buildPlaidifyHostedLinkUrl: () => buildPlaidifyHostedLinkUrl,
  createPlaidifyReactNativeMessageHandler: () => createPlaidifyReactNativeMessageHandler,
  createPlaidifyReactNativeWebViewProps: () => createPlaidifyReactNativeWebViewProps,
  isPlaidifyTerminalEvent: () => isPlaidifyTerminalEvent,
  parsePlaidifyLinkMessage: () => parsePlaidifyLinkMessage,
  shouldDismissPlaidifySheet: () => shouldDismissPlaidifySheet,
  usePlaidifyReactNativeLink: () => usePlaidifyReactNativeLink
});
module.exports = __toCommonJS(react_native_exports);
var import_react = __toESM(require("react"));

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
function trimTrailingSlashes(url) {
  let end = url.length;
  while (end > 0 && url.charCodeAt(end - 1) === 47) {
    end -= 1;
  }
  return url.slice(0, end);
}
function buildHostedLinkUrl(serverUrl, linkToken, options = {}, base) {
  const url = new URL(`${trimTrailingSlashes(serverUrl)}/link`, base);
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
  return new URL(trimTrailingSlashes(serverUrl)).origin;
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
  const [status, setStatus] = (0, import_react.useState)("idle");
  const [lastEvent, setLastEvent] = (0, import_react.useState)(null);
  const finishedRef = (0, import_react.useRef)(false);
  const url = (0, import_react.useMemo)(() => buildPlaidifyHostedLinkUrl(config), [config]);
  const { onEvent, onExit, onMFA, onSuccess, serverUrl } = config;
  const handleMessage = (0, import_react.useMemo)(
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
  const reset = (0, import_react.useCallback)(() => {
    finishedRef.current = false;
    setStatus("idle");
    setLastEvent(null);
  }, []);
  const webViewProps = (0, import_react.useMemo)(() => {
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
  return import_react.default.createElement(WebViewComponent, webViewProps);
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
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  PlaidifyReactNativeLink,
  buildPlaidifyHostedLinkUrl,
  createPlaidifyReactNativeMessageHandler,
  createPlaidifyReactNativeWebViewProps,
  isPlaidifyTerminalEvent,
  parsePlaidifyLinkMessage,
  shouldDismissPlaidifySheet,
  usePlaidifyReactNativeLink
});
//# sourceMappingURL=react-native.js.map