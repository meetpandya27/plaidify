// src/react.ts
import { useState, useCallback, useEffect, useRef } from "react";
function usePlaidifyLink(config) {
  const [status, setStatus] = useState("idle");
  const iframeRef = useRef(null);
  const overlayRef = useRef(null);
  const configRef = useRef(config);
  configRef.current = config;
  const cleanup = useCallback(() => {
    if (overlayRef.current) {
      document.body.removeChild(overlayRef.current);
      overlayRef.current = null;
    }
    iframeRef.current = null;
  }, []);
  const close = useCallback(() => {
    cleanup();
    setStatus("idle");
    configRef.current.onExit?.();
  }, [cleanup]);
  useEffect(() => {
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
  const open = useCallback(() => {
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
  useEffect(() => cleanup, [cleanup]);
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
export {
  PlaidifyLink,
  usePlaidifyLink
};
//# sourceMappingURL=react.mjs.map