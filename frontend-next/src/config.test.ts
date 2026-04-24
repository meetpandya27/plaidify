import { describe, expect, it } from "vitest";

import { detectNativeBridges, readHostedLinkConfig } from "./config";

describe("readHostedLinkConfig", () => {
  it("prefers the explicit ?origin= query parameter", () => {
    const config = readHostedLinkConfig(
      { search: "?token=tok&origin=https://merchant.example", origin: "https://host" },
      { referrer: "", inIframe: true },
    );
    expect(config.linkToken).toBe("tok");
    expect(config.parentOrigin).toBe("https://merchant.example");
    expect(config.serverUrl).toBe("https://host");
    expect(config.inIframe).toBe(true);
  });

  it("derives the parent origin from the referrer when no ?origin= is set", () => {
    const config = readHostedLinkConfig(
      { search: "?token=tok", origin: "https://host" },
      { referrer: "https://app.example/checkout?flow=1", inIframe: true },
    );
    expect(config.parentOrigin).toBe("https://app.example");
  });

  it("falls back to the server URL when embedded with no referrer", () => {
    const config = readHostedLinkConfig(
      { search: "?token=tok", origin: "https://host" },
      { referrer: "", inIframe: true },
    );
    expect(config.parentOrigin).toBe("https://host");
  });

  it("honours an explicit server override", () => {
    const config = readHostedLinkConfig(
      { search: "?token=tok&server=https://api.plaidify.test/", origin: "https://host" },
      { referrer: "", inIframe: false },
    );
    expect(config.serverUrl).toBe("https://api.plaidify.test");
  });
});

describe("detectNativeBridges", () => {
  it("picks up React Native WebView handlers", () => {
    const target = { ReactNativeWebView: { postMessage: () => undefined } } as unknown as typeof globalThis;
    const bridges = detectNativeBridges(target);
    expect(bridges.reactNative).not.toBeNull();
    expect(bridges.webkit).toBeNull();
  });

  it("picks up WKWebView handlers", () => {
    const target = {
      webkit: { messageHandlers: { plaidifyLink: { postMessage: () => undefined } } },
    } as unknown as typeof globalThis;
    const bridges = detectNativeBridges(target);
    expect(bridges.webkit).not.toBeNull();
    expect(bridges.reactNative).toBeNull();
  });

  it("returns both null when no bridge is present", () => {
    const bridges = detectNativeBridges({} as unknown as typeof globalThis);
    expect(bridges.reactNative).toBeNull();
    expect(bridges.webkit).toBeNull();
  });
});
