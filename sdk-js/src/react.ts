/**
 * React components for Plaidify Link integration.
 *
 * @example
 * ```tsx
 * import { PlaidifyLink, usePlaidifyLink } from "@plaidify/client/react";
 *
 * function App() {
 *   const { open, ready } = usePlaidifyLink({
 *     serverUrl: "http://localhost:8000",
 *     token: linkToken,
 *     onSuccess: (publicToken) => console.log("Got public token:", publicToken),
 *   });
 *
 *   return <button onClick={open} disabled={!ready}>Connect Account</button>;
 * }
 * ```
 */

import { useState, useCallback, useEffect, useRef } from "react";
import { sanitizeLinkPayload } from "./link-events";
import { buildHostedLinkUrl, trimTrailingSlashes } from "./link-url";
import type {
  PlaidifyLinkConfig,
  PlaidifyLinkEventPayload,
  PlaidifyLinkExitDetails,
} from "./types";

// ── Hook ─────────────────────────────────────────────────────────────────────

export interface UsePlaidifyLinkReturn {
  /** Open the link modal. Does nothing while it is already open. */
  open: () => void;
  /** Whether the link component is ready to open. */
  ready: boolean;
  /**
   * Current status of the link flow. "error" means the last event was a
   * recoverable ERROR: Link stays open on its retry screen.
   */
  status: "idle" | "loading" | "open" | "success" | "error";
  /** Close the link modal programmatically (reported as an exit). */
  close: () => void;
}

function serverOrigin(serverUrl: string): string | null {
  try {
    const base = typeof window !== "undefined" ? window.location.href : undefined;
    return new URL(trimTrailingSlashes(serverUrl), base).origin;
  } catch {
    return null;
  }
}

export function usePlaidifyLink(config: PlaidifyLinkConfig): UsePlaidifyLinkReturn {
  const [status, setStatus] = useState<UsePlaidifyLinkReturn["status"]>("idle");
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const resizeHandlerRef = useRef<(() => void) | null>(null);
  // True from open() until the session is over; guards every callback so
  // onSuccess/onExit fire at most once per open.
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

  /** End the session: tear the modal down and report how it ended, once. */
  const finish = useCallback(
    (outcome: { success: PlaidifyLinkEventPayload } | { exit: PlaidifyLinkExitDetails }) => {
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
    [cleanup],
  );

  const close = useCallback(() => {
    finish({ exit: { reason: "user_closed" } });
  }, [finish]);

  // Listen for postMessage events from the iframe
  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      if (!activeRef.current) return;
      // Only the Link iframe this hook opened, served from the Plaidify
      // server — not another frame on the page posting look-alike events.
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
            session_id: data.session_id,
          });
          break;
        case "EXIT":
        case "CLOSE":
        case "DONE":
          finish({
            exit: {
              reason: data.reason || String(data.event).toLowerCase(),
              error: data.error,
              error_code: data.error_code,
            },
          });
          break;
        case "ERROR":
          // Recoverable: the page offers retry / another provider. Link
          // closes only when the user exits.
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
      // Already showing: a second overlay would be orphaned on close.
      return;
    }
    const cfg = configRef.current;
    const url = buildHostedLinkUrl(
      cfg.serverUrl,
      cfg.token,
      { origin: window.location.origin, theme: cfg.theme },
      window.location.href,
    );
    activeRef.current = true;
    setStatus("loading");

    // Create overlay
    const overlay = document.createElement("div");
    overlay.style.cssText =
      "position:fixed;inset:0;z-index:999999;background:rgba(0,0,0,0.5);" +
      "display:flex;align-items:center;justify-content:center;padding:20px;";

    // Create iframe
    const iframe = document.createElement("iframe");
    iframe.src = url;
    iframe.title = "Plaidify Link";
    iframe.style.cssText =
      "width:min(100%,680px);max-width:680px;height:min(820px,92vh);max-height:92vh;border:none;" +
      `border-radius:${cfg.theme?.borderRadius || "30px"};` +
      "background:#fff;box-shadow:0 30px 90px rgba(15,23,42,0.28);";
    iframe.allow = "clipboard-write";
    iframe.onload = () => {
      if (activeRef.current) setStatus((current) => (current === "loading" ? "open" : current));
    };

    // Close on overlay click
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

  // Cleanup on unmount
  useEffect(
    () => () => {
      activeRef.current = false;
      cleanup();
    },
    [cleanup],
  );

  return {
    open,
    ready: !!config.token && !!config.serverUrl,
    status,
    close,
  };
}

// ── Component ────────────────────────────────────────────────────────────────

export interface PlaidifyLinkProps extends PlaidifyLinkConfig {
  children: (props: UsePlaidifyLinkReturn) => React.ReactElement;
}

/**
 * Render-prop component for Plaidify Link.
 *
 * @example
 * ```tsx
 * <PlaidifyLink serverUrl="..." token={token} onSuccess={handleSuccess}>
 *   {({ open, ready }) => (
 *     <button onClick={open} disabled={!ready}>Connect</button>
 *   )}
 * </PlaidifyLink>
 * ```
 */
export function PlaidifyLink({ children, ...config }: PlaidifyLinkProps) {
  const linkProps = usePlaidifyLink(config);
  return children(linkProps);
}
