import { describe, expect, it, vi } from "vitest";

import {
  buildPlaidifyHostedLinkUrl,
  createPlaidifyReactNativeMessageHandler,
  createPlaidifyReactNativeWebViewProps,
  isPlaidifyTerminalEvent,
  parsePlaidifyLinkMessage,
  shouldDismissPlaidifySheet,
} from "../src/react-native";

function nativeMessage(payload: Record<string, unknown>, url = "https://api.example.com/link?token=lnk-123") {
  return { nativeEvent: { url, data: JSON.stringify({ source: "plaidify-link", ...payload }) } };
}

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

  it("creates react native webview props limited to the Plaidify origin", () => {
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
    const payload = parsePlaidifyLinkMessage(
      nativeMessage({ event: "CONNECTED", public_token: "public-123" }),
    );

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

  it("keeps telemetry fields so hosts can forward them", () => {
    const payload = parsePlaidifyLinkMessage(
      JSON.stringify({ source: "plaidify-link", event: "TELEMETRY", name: "step_view", step: "mfa", elapsed_ms: 1200 }),
    );
    expect(payload).toMatchObject({ event: "TELEMETRY", name: "step_view", step: "mfa", elapsed_ms: 1200 });
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

    handleMessage(
      nativeMessage({
        event: "CONNECTED",
        public_token: "public-456",
        job_id: "job-123",
        data: { should_not_escape: true },
      }),
    );

    expect(successToken).toBe("public-456");
    expect(successMetadata?.public_token).toBe("public-456");
    expect(successMetadata?.job_id).toBe("job-123");
    expect(successMetadata).not.toHaveProperty("data");
  });

  it("treats ERROR as recoverable: no exit, the page shows its retry screen", () => {
    const onExit = vi.fn();
    const onEvent = vi.fn();
    const onStatusChange = vi.fn();
    const handleMessage = createPlaidifyReactNativeMessageHandler({ onExit, onEvent, onStatusChange });

    handleMessage(nativeMessage({ event: "ERROR", error: "bad password", error_code: "invalid_credentials" }));

    expect(onExit).not.toHaveBeenCalled();
    expect(onEvent).toHaveBeenCalledWith("ERROR", expect.objectContaining({ error_code: "invalid_credentials" }));
    expect(onStatusChange).toHaveBeenLastCalledWith("error");
  });

  it("reports an exit with the reason and last error code", () => {
    const onExit = vi.fn();
    const handleMessage = createPlaidifyReactNativeMessageHandler({ onExit });

    handleMessage(nativeMessage({ event: "EXIT", reason: "user_exit", error_code: "rate_limited" }));

    expect(onExit).toHaveBeenCalledWith({ reason: "user_exit", error: undefined, error_code: "rate_limited" });
  });

  it("drops messages from a page that is not on the Plaidify origin", () => {
    const onSuccess = vi.fn();
    const handleMessage = createPlaidifyReactNativeMessageHandler({
      onSuccess,
      expectedOrigin: "https://api.example.com",
    });

    const spoofed = handleMessage(
      nativeMessage({ event: "CONNECTED", public_token: "public-evil" }, "https://evil.example/page"),
    );
    expect(spoofed).toBeNull();
    expect(onSuccess).not.toHaveBeenCalled();

    handleMessage(nativeMessage({ event: "CONNECTED", public_token: "public-ok" }));
    expect(onSuccess).toHaveBeenCalledWith("public-ok", expect.anything());
  });

  it("detects terminal events", () => {
    expect(isPlaidifyTerminalEvent("CONNECTED")).toBe(true);
    expect(isPlaidifyTerminalEvent("EXIT")).toBe(true);
    expect(isPlaidifyTerminalEvent("MFA_REQUIRED")).toBe(false);
    expect(isPlaidifyTerminalEvent("ERROR")).toBe(false);
  });

  it("dismisses a mobile sheet on completion or exit, never on an error", () => {
    expect(shouldDismissPlaidifySheet({ source: "plaidify-link", event: "DONE" })).toBe(true);
    expect(shouldDismissPlaidifySheet({ source: "plaidify-link", event: "CONNECTED" })).toBe(true);
    expect(shouldDismissPlaidifySheet({ source: "plaidify-link", event: "EXIT" })).toBe(true);
    expect(shouldDismissPlaidifySheet({ source: "plaidify-link", event: "ERROR" })).toBe(false);
    expect(shouldDismissPlaidifySheet({ source: "plaidify-link", event: "MFA_REQUIRED" })).toBe(false);
  });
});
