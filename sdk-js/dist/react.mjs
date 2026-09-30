// src/react.ts
import { useState, useCallback, useEffect, useRef } from "react";

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

// src/react.ts
function serverOrigin(serverUrl) {
  try {
    const base = typeof window !== "undefined" ? window.location.href : void 0;
    return new URL(serverUrl.replace(/\/+$/, ""), base).origin;
  } catch {
    return null;
  }
}
function usePlaidifyLink(config) {
  const [status, setStatus] = useState("idle");
  const iframeRef = useRef(null);
  const overlayRef = useRef(null);
  const resizeHandlerRef = useRef(null);
  const activeRef = useRef(false);
  const configRef = useRef(config);
  configRef.current = config;
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
    overlayRef.current?.remove();
    overlayRef.current = null;
    iframeRef.current = null;
  }, []);
  const finish = useCallback(
    (outcome) => {
      if (!activeRef.current) {
        return;
      }
      activeRef.current = false;
      cleanup();
      if ("success" in outcome) {
        setStatus("success");
        configRef.current.onSuccess?.(outcome.success.public_token || "", outcome.success);
      } else {
        setStatus("idle");
        configRef.current.onExit?.(outcome.exit);
      }
    },
    [cleanup]
  );
  const close = useCallback(() => {
    finish({ exit: { reason: "user_closed" } });
  }, [finish]);
  useEffect(() => {
    function handleMessage(event) {
      if (!activeRef.current) return;
      const frame = iframeRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      if (event.origin !== serverOrigin(configRef.current.serverUrl)) return;
      const data = sanitizeLinkPayload(event.data);
      if (!data) return;
      configRef.current.onEvent?.(data.event || "UNKNOWN", data);
      switch (data.event) {
        case "CONNECTED":
          finish({ success: data });
          break;
        case "MFA_REQUIRED":
          setStatus("open");
          configRef.current.onMFA?.({
            mfa_type: data.mfa_type,
            session_id: data.session_id
          });
          break;
        case "EXIT":
        case "CLOSE":
        case "DONE":
          finish({
            exit: {
              reason: data.reason || String(data.event).toLowerCase(),
              error: data.error,
              error_code: data.error_code
            }
          });
          break;
        case "ERROR":
          setStatus("error");
          break;
        case "TELEMETRY":
          break;
        default:
          setStatus("open");
          break;
      }
    }
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [finish]);
  const open = useCallback(() => {
    if (activeRef.current) {
      return;
    }
    const cfg = configRef.current;
    const url = buildHostedLinkUrl(
      cfg.serverUrl,
      cfg.token,
      { origin: window.location.origin, theme: cfg.theme },
      window.location.href
    );
    activeRef.current = true;
    setStatus("loading");
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;z-index:999999;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;padding:20px;";
    const iframe = document.createElement("iframe");
    iframe.src = url;
    iframe.title = "Plaidify Link";
    iframe.style.cssText = `width:min(100%,680px);max-width:680px;height:min(820px,92vh);max-height:92vh;border:none;border-radius:${cfg.theme?.borderRadius || "30px"};background:#fff;box-shadow:0 30px 90px rgba(15,23,42,0.28);`;
    iframe.allow = "clipboard-write";
    iframe.onload = () => {
      if (activeRef.current) setStatus((current) => current === "loading" ? "open" : current);
    };
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
  }, [applyResponsiveLayout, close]);
  useEffect(
    () => () => {
      activeRef.current = false;
      cleanup();
    },
    [cleanup]
  );
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