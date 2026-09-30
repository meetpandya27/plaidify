// @vitest-environment jsdom
/**
 * The web modal hook: which messages it trusts, when it closes, and how
 * often it reports the outcome.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

import { usePlaidifyLink } from "../src/react";
import type { PlaidifyLinkConfig } from "../src/types";

const SERVER = "https://link.plaidify.test";

function setup(overrides: Partial<PlaidifyLinkConfig> = {}) {
  const callbacks = {
    onSuccess: vi.fn(),
    onExit: vi.fn(),
    onEvent: vi.fn(),
    onMFA: vi.fn(),
  };
  const hook = renderHook(() =>
    usePlaidifyLink({ serverUrl: SERVER, token: "lnk-1", ...callbacks, ...overrides }),
  );
  return { hook, ...callbacks };
}

function overlays(): HTMLDivElement[] {
  return Array.from(document.body.children).filter(
    (node): node is HTMLDivElement => node instanceof HTMLDivElement && !!node.querySelector("iframe"),
  );
}

function linkFrame(): HTMLIFrameElement {
  const frame = document.querySelector("iframe");
  if (!frame) throw new Error("Link is not open");
  return frame;
}

function post(
  data: Record<string, unknown>,
  options: { origin?: string; source?: Window | null } = {},
) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { source: "plaidify-link", ...data },
        origin: options.origin ?? SERVER,
        source: options.source === undefined ? linkFrame().contentWindow : options.source,
      }),
    );
  });
}

describe("usePlaidifyLink", () => {
  afterEach(() => {
    cleanup();
    document.body.innerHTML = "";
  });

  it("opens one overlay however often open() is called", () => {
    const { hook } = setup();
    act(() => {
      hook.result.current.open();
      hook.result.current.open();
    });
    expect(overlays()).toHaveLength(1);

    const src = new URL(linkFrame().src);
    expect(src.origin + src.pathname).toBe(`${SERVER}/link`);
    expect(src.searchParams.get("token")).toBe("lnk-1");
    expect(src.searchParams.get("origin")).toBe(window.location.origin);
  });

  it("passes the theme to the page as the parameters it reads", () => {
    const { hook } = setup({ theme: { accentColor: "#0b8f73", bgColor: "#eef5ff", borderRadius: "20px" } });
    act(() => hook.result.current.open());
    const params = new URL(linkFrame().src).searchParams;
    expect(params.get("accent")).toBe("#0b8f73");
    expect(params.get("bg")).toBe("#eef5ff");
    expect(params.get("radius")).toBe("20px");
  });

  it("ignores messages that do not come from the Link iframe", () => {
    const { hook, onSuccess, onEvent } = setup();
    act(() => hook.result.current.open());

    // Right origin, but posted by the host page itself (or another frame).
    post({ event: "CONNECTED", public_token: "public-spoof" }, { source: window });
    // The Link frame, but not from the Plaidify origin.
    post({ event: "CONNECTED", public_token: "public-spoof" }, { origin: "https://evil.example" });

    expect(onSuccess).not.toHaveBeenCalled();
    expect(onEvent).not.toHaveBeenCalled();
    expect(overlays()).toHaveLength(1);
  });

  it("stays open on ERROR so the user can retry or pick another provider", () => {
    const { hook, onExit, onEvent } = setup();
    act(() => hook.result.current.open());

    post({ event: "ERROR", error: "bad password", error_code: "invalid_credentials" });

    expect(onEvent).toHaveBeenCalledWith("ERROR", expect.objectContaining({ error_code: "invalid_credentials" }));
    expect(onExit).not.toHaveBeenCalled();
    expect(overlays()).toHaveLength(1);
    expect(hook.result.current.status).toBe("error");
  });

  it("reports an exit once, with its reason, and closes", () => {
    const { hook, onExit } = setup();
    act(() => hook.result.current.open());
    const frameWindow = linkFrame().contentWindow;

    post({ event: "EXIT", reason: "user_exit", error_code: "rate_limited" });
    post({ event: "EXIT", reason: "page_closed" }, { source: frameWindow });
    act(() => hook.result.current.close());

    expect(onExit).toHaveBeenCalledTimes(1);
    expect(onExit).toHaveBeenCalledWith({ reason: "user_exit", error: undefined, error_code: "rate_limited" });
    expect(overlays()).toHaveLength(0);
    expect(hook.result.current.status).toBe("idle");
  });

  it("reports success once and never an exit afterwards", () => {
    const { hook, onSuccess, onExit } = setup();
    act(() => hook.result.current.open());
    const frameWindow = linkFrame().contentWindow;

    post({ event: "CONNECTED", public_token: "public-1", job_id: "job-1" });
    post({ event: "EXIT", reason: "page_closed" }, { source: frameWindow });

    expect(onSuccess).toHaveBeenCalledTimes(1);
    expect(onSuccess).toHaveBeenCalledWith("public-1", expect.objectContaining({ job_id: "job-1" }));
    expect(onExit).not.toHaveBeenCalled();
    expect(overlays()).toHaveLength(0);
    expect(hook.result.current.status).toBe("success");
  });

  it("reports a host-side close as a single exit", () => {
    const { hook, onExit } = setup();
    act(() => hook.result.current.open());
    act(() => {
      hook.result.current.close();
      hook.result.current.close();
    });
    expect(onExit).toHaveBeenCalledTimes(1);
    expect(onExit).toHaveBeenCalledWith({ reason: "user_closed" });
    expect(overlays()).toHaveLength(0);
  });

  it("can open again after closing", () => {
    const { hook, onExit } = setup();
    act(() => hook.result.current.open());
    act(() => hook.result.current.close());
    act(() => hook.result.current.open());
    expect(overlays()).toHaveLength(1);

    post({ event: "EXIT", reason: "user_exit" });
    expect(onExit).toHaveBeenCalledTimes(2);
  });

  it("removes the overlay when the host component unmounts", () => {
    const { hook } = setup();
    act(() => hook.result.current.open());
    hook.unmount();
    expect(overlays()).toHaveLength(0);
  });
});
