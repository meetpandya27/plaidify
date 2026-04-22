// src/react.ts
import { useState, useCallback, useEffect, useRef } from "react";
function sanitizePlaidifyLinkPayload(data) {
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
    job_id: payload.job_id,
    mfa_type: payload.mfa_type,
    organization_id: payload.organization_id,
    organization_name: payload.organization_name,
    public_token: payload.public_token,
    reason: payload.reason,
    session_id: payload.session_id,
    site: payload.site
  };
}
function usePlaidifyLink(config) {
  const [status, setStatus] = useState("idle");
  const iframeRef = useRef(null);
  const overlayRef = useRef(null);
  const resizeHandlerRef = useRef(null);
  const serverOriginRef = useRef(new URL(config.serverUrl.replace(/\/+$/, ""), window.location.href).origin);
  const configRef = useRef(config);
  configRef.current = config;
  serverOriginRef.current = new URL(config.serverUrl.replace(/\/+$/, ""), window.location.href).origin;
  const applyResponsiveLayout = useCallback(() => {
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
  const cleanup = useCallback(() => {
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
  const close = useCallback(() => {
    cleanup();
    setStatus("idle");
    configRef.current.onExit?.({ reason: "user_closed" });
  }, [cleanup]);
  useEffect(() => {
    function handleMessage(event) {
      const data = sanitizePlaidifyLinkPayload(event.data);
      if (!data || data.source !== "plaidify-link") return;
      if (event.origin !== serverOriginRef.current) return;
      configRef.current.onEvent?.(data.event || "UNKNOWN", data);
      switch (data.event) {
        case "CONNECTED":
          setStatus("success");
          cleanup();
          configRef.current.onSuccess?.(data.public_token || "", data);
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