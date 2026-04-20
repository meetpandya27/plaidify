"use strict";
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
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
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// src/react.ts
var react_exports = {};
__export(react_exports, {
  PlaidifyLink: () => PlaidifyLink,
  usePlaidifyLink: () => usePlaidifyLink
});
module.exports = __toCommonJS(react_exports);
var import_react = require("react");
function usePlaidifyLink(config) {
  const [status, setStatus] = (0, import_react.useState)("idle");
  const iframeRef = (0, import_react.useRef)(null);
  const overlayRef = (0, import_react.useRef)(null);
  const resizeHandlerRef = (0, import_react.useRef)(null);
  const configRef = (0, import_react.useRef)(config);
  configRef.current = config;
  const applyResponsiveLayout = (0, import_react.useCallback)(() => {
    if (!overlayRef.current || !iframeRef.current) {
      return;
    }
    const theme = configRef.current.theme;
    const breakpoint = theme?.mobileBreakpoint ?? 768;
    const shouldFullscreen = theme?.fullscreenOnMobile !== false && window.innerWidth <= breakpoint;
    if (shouldFullscreen) {
      overlayRef.current.style.padding = "0";
      overlayRef.current.style.alignItems = "stretch";
      overlayRef.current.style.justifyContent = "stretch";
      iframeRef.current.style.width = "100vw";
      iframeRef.current.style.maxWidth = "100vw";
      iframeRef.current.style.height = "100vh";
      iframeRef.current.style.maxHeight = "100vh";
      iframeRef.current.style.borderRadius = "0";
      iframeRef.current.style.boxShadow = "none";
      return;
    }
    overlayRef.current.style.padding = "20px";
    overlayRef.current.style.alignItems = "center";
    overlayRef.current.style.justifyContent = "center";
    iframeRef.current.style.width = "min(100%, 680px)";
    iframeRef.current.style.maxWidth = "680px";
    iframeRef.current.style.height = "min(820px, 92vh)";
    iframeRef.current.style.maxHeight = "92vh";
    iframeRef.current.style.borderRadius = theme?.borderRadius || "30px";
    iframeRef.current.style.boxShadow = "0 30px 90px rgba(15, 23, 42, 0.28)";
  }, []);
  const cleanup = (0, import_react.useCallback)(() => {
    if (resizeHandlerRef.current) {
      window.removeEventListener("resize", resizeHandlerRef.current);
      resizeHandlerRef.current = null;
    }
    if (overlayRef.current) {
      document.body.removeChild(overlayRef.current);
      overlayRef.current = null;
    }
    iframeRef.current = null;
  }, []);
  const close = (0, import_react.useCallback)(() => {
    cleanup();
    setStatus("idle");
    configRef.current.onExit?.({ reason: "user_closed" });
  }, [cleanup]);
  (0, import_react.useEffect)(() => {
    function handleMessage(event) {
      const data = event.data;
      if (!data || data.source !== "plaidify-link") return;
      configRef.current.onEvent?.(data.event, data);
      switch (data.event) {
        case "CONNECTED":
          setStatus("success");
          cleanup();
          configRef.current.onSuccess?.(data.access_token || "", data);
          break;
        case "MFA_REQUIRED":
          configRef.current.onMFA?.({
            mfa_type: data.mfa_type,
            session_id: data.session_id
          });
          break;
        case "EXIT":
        case "CLOSE":
          close();
          break;
        case "ERROR":
          setStatus("error");
          cleanup();
          configRef.current.onExit?.({ reason: "error", error: data.error || "Link error" });
          break;
      }
    }
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [cleanup, close]);
  const open = (0, import_react.useCallback)(() => {
    const cfg = configRef.current;
    setStatus("loading");
    let url = `${cfg.serverUrl}/link?token=${encodeURIComponent(cfg.token)}`;
    url += `&origin=${encodeURIComponent(window.location.origin)}`;
    if (cfg.theme?.accentColor) url += `&accent=${encodeURIComponent(cfg.theme.accentColor)}`;
    if (cfg.theme?.bgColor) url += `&bg=${encodeURIComponent(cfg.theme.bgColor)}`;
    if (cfg.theme?.borderRadius) url += `&radius=${encodeURIComponent(cfg.theme.borderRadius)}`;
    if (cfg.theme?.logo) url += `&logo=${encodeURIComponent(cfg.theme.logo)}`;
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;z-index:999999;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;padding:20px;";
    const iframe = document.createElement("iframe");
    iframe.src = url;
    iframe.style.cssText = `width:min(100%,680px);max-width:680px;height:min(820px,92vh);max-height:92vh;border:none;border-radius:${cfg.theme?.borderRadius || "30px"};background:#fff;box-shadow:0 30px 90px rgba(15,23,42,0.28);`;
    iframe.allow = "clipboard-write";
    iframe.onload = () => setStatus("open");
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    overlay.appendChild(iframe);
    document.body.appendChild(overlay);
    overlayRef.current = overlay;
    iframeRef.current = iframe;
    resizeHandlerRef.current = applyResponsiveLayout;
    window.addEventListener("resize", resizeHandlerRef.current);
    applyResponsiveLayout();
  }, [close]);
  (0, import_react.useEffect)(() => cleanup, [cleanup]);
  return {
    open,
    ready: !!config.token && !!config.serverUrl,
    status,
    close
  };
}
function PlaidifyLink({ children, ...config }) {
  const linkProps = usePlaidifyLink(config);
  return children(linkProps);
}
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  PlaidifyLink,
  usePlaidifyLink
});
//# sourceMappingURL=react.js.map