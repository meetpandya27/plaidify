/**
 * Read config from URL query string + runtime globals.
 *
 * The hosted-link page is loaded as `/link?token=...&server=...&origin=...`.
 * We also sniff the parent origin from document.referrer so embedders
 * don't have to pass it explicitly.
 */
export interface HostedLinkConfig {
  readonly linkToken: string | null;
  readonly serverUrl: string;
  readonly parentOrigin: string;
  readonly inIframe: boolean;
}

export function readHostedLinkConfig(
  location: Pick<Location, "search" | "origin">,
  options: {
    readonly referrer: string;
    readonly inIframe: boolean;
  },
): HostedLinkConfig {
  const params = new URLSearchParams(location.search);
  const linkToken = params.get("token");
  const serverUrl = (params.get("server") || location.origin).replace(/\/$/, "");
  const explicitOrigin = params.get("origin");

  let parentOrigin: string;
  if (explicitOrigin) {
    parentOrigin = explicitOrigin;
  } else if (options.referrer) {
    try {
      parentOrigin = new URL(options.referrer).origin;
    } catch {
      parentOrigin = options.inIframe ? serverUrl : "*";
    }
  } else {
    parentOrigin = options.inIframe ? serverUrl : "*";
  }

  return {
    linkToken,
    serverUrl,
    parentOrigin,
    inIframe: options.inIframe,
  };
}

export interface ReactNativeBridge {
  readonly postMessage: (payload: string) => void;
}

export interface WebkitBridge {
  readonly postMessage: (payload: Record<string, unknown>) => void;
}

export interface DetectedBridges {
  readonly reactNative: ReactNativeBridge | null;
  readonly webkit: WebkitBridge | null;
}

export function detectNativeBridges(target: typeof globalThis): DetectedBridges {
  let reactNative: ReactNativeBridge | null = null;
  let webkit: WebkitBridge | null = null;

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

  return { reactNative, webkit };
}
