import { describe, expect, it } from "vitest";

import {
  buildPlaidifyHostedLinkUrl,
  createPlaidifyReactNativeWebViewProps,
  isPlaidifyTerminalEvent,
  parsePlaidifyLinkMessage,
  shouldDismissPlaidifySheet,
} from "../src/react-native";

describe("react-native helpers", () => {
  it("builds a hosted link url with origin and theme", () => {
    const url = buildPlaidifyHostedLinkUrl({
      serverUrl: "https://api.example.com/",
      token: "lnk-123",
      origin: "myapp://callback",
      theme: { accentColor: "#0b8f73", borderRadius: "30px" },
    });

    expect(url).toContain("https://api.example.com/link?token=lnk-123");
    expect(url).toContain("origin=myapp%3A%2F%2Fcallback");
    expect(url).toContain("accent=%230b8f73");
    expect(url).toContain("radius=30px");
  });

  it("creates react native webview props", () => {
    const props = createPlaidifyReactNativeWebViewProps({
      serverUrl: "https://api.example.com",
      token: "lnk-123",
    });

    expect(props.source.uri).toBe("https://api.example.com/link?token=lnk-123");
    expect(props.javaScriptEnabled).toBe(true);
    expect(props.domStorageEnabled).toBe(true);
  });

  it("parses a react native onMessage payload", () => {
    const payload = parsePlaidifyLinkMessage({
      nativeEvent: {
        data: JSON.stringify({
          source: "plaidify-link",
          event: "CONNECTED",
          access_token: "acc-123",
        }),
      },
    });

    expect(payload?.event).toBe("CONNECTED");
    expect(payload?.access_token).toBe("acc-123");
  });

  it("rejects non-plaidify bridge messages", () => {
    const payload = parsePlaidifyLinkMessage(JSON.stringify({ event: "CONNECTED" }));
    expect(payload).toBeNull();
  });

  it("detects terminal events", () => {
    expect(isPlaidifyTerminalEvent("CONNECTED")).toBe(true);
    expect(isPlaidifyTerminalEvent("MFA_REQUIRED")).toBe(false);
  });

  it("indicates when a mobile sheet should dismiss", () => {
    expect(
      shouldDismissPlaidifySheet({ source: "plaidify-link", event: "DONE" }),
    ).toBe(true);
    expect(
      shouldDismissPlaidifySheet({ source: "plaidify-link", event: "MFA_REQUIRED" }),
    ).toBe(false);
  });
});