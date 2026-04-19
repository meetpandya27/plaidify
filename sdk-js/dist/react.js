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
  const configRef = (0, import_react.useRef)(config);
  configRef.current = config;
  const cleanup = (0, import_react.useCallback)(() => {
    if (overlayRef.current) {
      document.body.removeChild(overlayRef.current);
      overlayRef.current = null;
    }
    iframeRef.current = null;
  }, []);
  const close = (0, import_react.useCallback)(() => {
    cleanup();
    setStatus("idle");
    configRef.current.onExit?.();
  }, [cleanup]);
  (0, import_react.useEffect)(() => {
    function handleMessage(event) {
      const data = event.data;
      if (!data || data.source !== "plaidify-link") return;
      configRef.current.onEvent?.(data.event, data);
      switch (data.event) {
        case "SUCCESS":
        case "LINK_COMPLETE":
          setStatus("success");
          cleanup();
          configRef.current.onSuccess?.(data.public_token, data);
          break;
        case "EXIT":
        case "CLOSE":
          close();
          break;
        case "ERROR":
          setStatus("error");
          cleanup();
          configRef.current.onExit?.(data.error || "Link error");
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
    overlay.style.cssText = "position:fixed;inset:0;z-index:999999;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;";
    const iframe = document.createElement("iframe");
    iframe.src = url;
    iframe.style.cssText = `width:420px;max-width:95vw;height:640px;max-height:90vh;border:none;border-radius:${cfg.theme?.borderRadius || "12px"};background:#fff;box-shadow:0 20px 60px rgba(0,0,0,0.3);`;
    iframe.allow = "clipboard-write";
    iframe.onload = () => setStatus("open");
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    overlay.appendChild(iframe);
    document.body.appendChild(overlay);
    overlayRef.current = overlay;
    iframeRef.current = iframe;
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