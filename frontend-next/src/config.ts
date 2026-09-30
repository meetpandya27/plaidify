/**
 * Read config from URL query string + runtime globals.
 *
 * The hosted-link page is loaded as `/link?token=...&origin=...`. We also
 * sniff the parent origin from document.referrer so embedders don't have
 * to pass it explicitly. Neither value decides where events may go: the
 * session's `allowed_origins` does (see ParentChannel in events.ts).
 */
export type LinkTokenProblem = "missing" | "duplicate";

export interface HostedLinkConfig {
  readonly linkToken: string | null;
  /** Why `linkToken` is null, when it is. */
  readonly tokenProblem: LinkTokenProblem | null;
  readonly serverUrl: string;
  /** Origin the page itself was served from. */
  readonly ownOrigin: string;
  /**
   * Best guess at the embedding window's origin (from `?origin=` or the
   * referrer). Only a hint: it is never used as a postMessage target
   * unless the session allows it, and it is never "*".
   */
  readonly parentOrigin: string | null;
  readonly inIframe: boolean;
}

export function readHostedLinkConfig(
  location: Pick<Location, "search" | "origin">,
  options: {
    readonly referrer: string;
    readonly inIframe: boolean;
    /**
     * Honour `?server=`. Only the Vite dev server needs it; a built page is
     * served by the API it talks to, and a `server` pointing anywhere else
     * would send the user's credentials there.
     */
    readonly allowServerOverride?: boolean;
  },
): HostedLinkConfig {
  const params = new URLSearchParams(location.search);

  // The server frames the page from the session named by one token; a
  // second `token` lets a page and the server disagree about which session
  // that is, so a link carrying more than one is not a link at all.
  const tokens = params.getAll("token");
  let linkToken: string | null = null;
  let tokenProblem: LinkTokenProblem | null = null;
  if (tokens.length > 1) {
    tokenProblem = "duplicate";
  } else if (!tokens[0]) {
    tokenProblem = "missing";
  } else {
    linkToken = tokens[0];
  }

  const ownOrigin = location.origin;
  const override = options.allowServerOverride ? params.get("server") : null;
  const serverUrl = (override || ownOrigin).replace(/\/$/, "");

  let parentOrigin: string | null = null;
  if (options.inIframe) {
    parentOrigin =
      normalizeOrigin(params.get("origin")) ??
      normalizeOrigin(options.referrer) ??
      normalizeOrigin(ownOrigin);
  }

  return {
    linkToken,
    tokenProblem,
    serverUrl,
    ownOrigin,
    parentOrigin,
    inIframe: options.inIframe,
  };
}

/**
 * Reduce a URL or origin string to a comparable `scheme://host[:port]`
 * origin. Anything that is not http(s) — "*", "null", custom schemes —
 * yields null so it can never become a postMessage target.
 */
export function normalizeOrigin(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") {
      return null;
    }
    return url.origin;
  } catch {
    return null;
  }
}

export interface ReactNativeBridge {
  readonly postMessage: (payload: string) => void;
}

export interface WebkitBridge {
  readonly postMessage: (payload: Record<string, unknown>) => void;
}

/** `window.plaidifyLink`, injected by the Android SDK's WebView. */
export interface AndroidBridge {
  readonly postMessage: (payload: string) => void;
}

export interface DetectedBridges {
  readonly reactNative: ReactNativeBridge | null;
  readonly webkit: WebkitBridge | null;
  readonly android: AndroidBridge | null;
}

export function detectNativeBridges(target: typeof globalThis): DetectedBridges {
  let reactNative: ReactNativeBridge | null = null;
  let webkit: WebkitBridge | null = null;
  let android: AndroidBridge | null = null;

  const rn = (target as unknown as {
    ReactNativeWebView?: { postMessage?: (payload: string) => void };
  }).ReactNativeWebView;
  if (rn && typeof rn.postMessage === "function") {
    reactNative = { postMessage: rn.postMessage.bind(rn) };
  }

  const webkitHandlers = (target as unknown as {
    webkit?: {
      messageHandlers?: {
        plaidifyLink?: { postMessage?: (payload: unknown) => void };
      };
    };
  }).webkit?.messageHandlers?.plaidifyLink;

  if (webkitHandlers && typeof webkitHandlers.postMessage === "function") {
    webkit = {
      postMessage: webkitHandlers.postMessage.bind(webkitHandlers) as (
        payload: Record<string, unknown>,
      ) => void,
    };
  }

  // Android exposes the bridge as a global object whose postMessage takes a
  // JSON string (WebViewCompat.addWebMessageListener, or a
  // @JavascriptInterface on older WebViews).
  const androidBridge = (target as unknown as {
    plaidifyLink?: { postMessage?: (payload: string) => void };
  }).plaidifyLink;
  if (androidBridge && typeof androidBridge.postMessage === "function") {
    android = { postMessage: androidBridge.postMessage.bind(androidBridge) };
  }

  return { reactNative, webkit, android };
}
