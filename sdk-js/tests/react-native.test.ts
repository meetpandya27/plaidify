import { describe, expect, it } from "vitest";

import {
  buildPlaidifyHostedLinkUrl,
  createPlaidifyReactNativeMessageHandler,
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
    expect(props.originWhitelist).toEqual(["https://api.example.com"]);
    expect(props.javaScriptEnabled).toBe(true);
    expect(props.domStorageEnabled).toBe(true);
  });

  it("parses a react native onMessage payload", () => {
    const payload = parsePlaidifyLinkMessage({
      nativeEvent: {
        data: JSON.stringify({
          source: "plaidify-link",
          event: "CONNECTED",
          public_token: "public-123",
        }),
      },
    });

    expect(payload?.event).toBe("CONNECTED");
    expect(payload?.public_token).toBe("public-123");
  });

  it("rejects non-plaidify bridge messages", () => {
    const payload = parsePlaidifyLinkMessage(JSON.stringify({ event: "CONNECTED" }));
    expect(payload).toBeNull();
  });

  it("sanitizes extra fields from bridge messages", () => {
    const payload = parsePlaidifyLinkMessage(
      JSON.stringify({
        source: "plaidify-link",
        event: "CONNECTED",
        public_token: "public-123",
        data: { balance: 42 },
        access_token: "secret",
      }),
    ) as Record<string, unknown> | null;

    expect(payload?.public_token).toBe("public-123");
    expect(payload).not.toHaveProperty("data");
    expect(payload).not.toHaveProperty("access_token");
  });

  it("passes only approved metadata to onSuccess", () => {
    let successToken = "";
    let successMetadata: Record<string, unknown> | null = null;
    const handleMessage = createPlaidifyReactNativeMessageHandler({
      onSuccess: (publicToken, metadata) => {
        successToken = publicToken;
        successMetadata = metadata as Record<string, unknown>;
      },
    });

    handleMessage({
      nativeEvent: {
        data: JSON.stringify({
          source: "plaidify-link",
          event: "CONNECTED",
          public_token: "public-456",
          job_id: "job-123",
          data: { should_not_escape: true },
        }),
      },
    });

    expect(successToken).toBe("public-456");
    expect(successMetadata?.public_token).toBe("public-456");
    expect(successMetadata?.job_id).toBe("job-123");
    expect(successMetadata).not.toHaveProperty("data");
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