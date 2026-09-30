import { describe, expect, it } from "vitest";

import { detectNativeBridges, normalizeOrigin, readHostedLinkConfig } from "./config";

describe("readHostedLinkConfig", () => {
  it("prefers the explicit ?origin= query parameter", () => {
    const config = readHostedLinkConfig(
      { search: "?token=tok&origin=https://merchant.example", origin: "https://host" },
      { referrer: "", inIframe: true },
    );
    expect(config.linkToken).toBe("tok");
    expect(config.tokenProblem).toBeNull();
    expect(config.parentOrigin).toBe("https://merchant.example");
    expect(config.serverUrl).toBe("https://host");
    expect(config.ownOrigin).toBe("https://host");
    expect(config.inIframe).toBe(true);
  });

  it("derives the parent origin from the referrer when no ?origin= is set", () => {
    const config = readHostedLinkConfig(
      { search: "?token=tok", origin: "https://host" },
      { referrer: "https://app.example/checkout?flow=1", inIframe: true },
    );
    expect(config.parentOrigin).toBe("https://app.example");
  });

  it("falls back to its own origin when embedded with no referrer", () => {
    const config = readHostedLinkConfig(
      { search: "?token=tok", origin: "https://host" },
      { referrer: "", inIframe: true },
    );
    expect(config.parentOrigin).toBe("https://host");
  });

  it("never guesses a parent origin, let alone '*', outside a frame", () => {
    const config = readHostedLinkConfig(
      { search: "?token=tok&origin=https://merchant.example", origin: "https://host" },
      { referrer: "https://merchant.example/", inIframe: false },
    );
    expect(config.parentOrigin).toBeNull();
  });

  it("ignores an ?origin= that is not an http(s) origin", () => {
    for (const bad of ["*", "null", "myapp://callback", "javascript:alert(1)"]) {
      const config = readHostedLinkConfig(
        { search: `?token=tok&origin=${encodeURIComponent(bad)}`, origin: "https://host" },
        { referrer: "https://app.example/", inIframe: true },
      );
      expect(config.parentOrigin).toBe("https://app.example");
    }
  });

  it("rejects a link that carries more than one token", () => {
    const config = readHostedLinkConfig(
      { search: "?token=victim&token=attacker", origin: "https://host" },
      { referrer: "", inIframe: true },
    );
    expect(config.linkToken).toBeNull();
    expect(config.tokenProblem).toBe("duplicate");
  });

  it("reports a missing or empty token", () => {
    for (const search of ["", "?token=", "?origin=https://a.example"]) {
      const config = readHostedLinkConfig(
        { search, origin: "https://host" },
        { referrer: "", inIframe: false },
      );
      expect(config.linkToken).toBeNull();
      expect(config.tokenProblem).toBe("missing");
    }
  });

  it("honours ?server= only when the dev override is allowed", () => {
    const location = {
      search: "?token=tok&server=https://api.plaidify.test/",
      origin: "https://host",
    };
    expect(
      readHostedLinkConfig(location, {
        referrer: "",
        inIframe: false,
        allowServerOverride: true,
      }).serverUrl,
    ).toBe("https://api.plaidify.test");
    // A built page talks only to the origin that served it.
    expect(
      readHostedLinkConfig(location, { referrer: "", inIframe: false }).serverUrl,
    ).toBe("https://host");
  });
});

describe("normalizeOrigin", () => {
  it("reduces URLs to their origin and refuses non-http schemes", () => {
    expect(normalizeOrigin("https://App.Example:443/path?q=1")).toBe("https://app.example");
    expect(normalizeOrigin("http://localhost:3000/")).toBe("http://localhost:3000");
    expect(normalizeOrigin("*")).toBeNull();
    expect(normalizeOrigin("file:///etc/passwd")).toBeNull();
    expect(normalizeOrigin("")).toBeNull();
    expect(normalizeOrigin(null)).toBeNull();
  });
});

describe("detectNativeBridges", () => {
  it("picks up React Native WebView handlers", () => {
    const target = { ReactNativeWebView: { postMessage: () => undefined } } as unknown as typeof globalThis;
    const bridges = detectNativeBridges(target);
    expect(bridges.reactNative).not.toBeNull();
    expect(bridges.webkit).toBeNull();
    expect(bridges.android).toBeNull();
  });

  it("picks up WKWebView handlers", () => {
    const target = {
      webkit: { messageHandlers: { plaidifyLink: { postMessage: () => undefined } } },
    } as unknown as typeof globalThis;
    const bridges = detectNativeBridges(target);
    expect(bridges.webkit).not.toBeNull();
    expect(bridges.reactNative).toBeNull();
    expect(bridges.android).toBeNull();
  });

  it("picks up the Android window.plaidifyLink bridge", () => {
    const received: string[] = [];
    const target = {
      plaidifyLink: { postMessage: (payload: string) => received.push(payload) },
    } as unknown as typeof globalThis;
    const bridges = detectNativeBridges(target);
    expect(bridges.android).not.toBeNull();
    bridges.android?.postMessage('{"event":"OPEN"}');
    expect(received).toEqual(['{"event":"OPEN"}']);
  });

  it("returns null for every bridge when none is present", () => {
    const bridges = detectNativeBridges({} as unknown as typeof globalThis);
    expect(bridges.reactNative).toBeNull();
    expect(bridges.webkit).toBeNull();
    expect(bridges.android).toBeNull();
  });
});
