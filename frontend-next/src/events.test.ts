import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EventDelivery, postBridgeEvent } from "./events";

type FetchMock = ReturnType<typeof vi.fn>;

interface TestTimer {
  readonly cb: () => void;
  readonly delayMs: number;
}

function makeScheduler() {
  const timers: TestTimer[] = [];
  return {
    timers,
    setTimer: (cb: () => void, delayMs: number) => {
      timers.push({ cb, delayMs });
      return timers.length - 1;
    },
    clearTimer: (_handle: unknown) => {
      // Not important for these tests.
    },
    run: () => {
      const pending = timers.splice(0, timers.length);
      for (const timer of pending) {
        timer.cb();
      }
    },
  };
}

async function flush(): Promise<void> {
  // Drain all pending microtasks between fetch retries.
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
  }
}

describe("EventDelivery", () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("delivers a single event exactly once on success", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    const delivery = new EventDelivery({
      linkToken: "tok",
      serverUrl: "https://api.plaidify.test/",
      fetchImpl: fetchMock as unknown as typeof fetch,
      setTimer: () => -1,
      clearTimer: () => undefined,
    });

    delivery.enqueue("OPEN", { client: "react" });
    await flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://api.plaidify.test/link/sessions/tok/event");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      event: "OPEN",
      client: "react",
    });
    expect(delivery.pending).toBe(0);
  });

  it("retries with exponential backoff and preserves queue order", async () => {
    const scheduler = makeScheduler();
    fetchMock
      .mockRejectedValueOnce(new Error("network down"))
      .mockRejectedValueOnce(new Error("still down"))
      .mockResolvedValue(new Response(null, { status: 204 }));

    const delivery = new EventDelivery({
      linkToken: "tok",
      serverUrl: "https://api.plaidify.test",
      fetchImpl: fetchMock as unknown as typeof fetch,
      setTimer: scheduler.setTimer,
      clearTimer: scheduler.clearTimer,
    });

    delivery.enqueue("OPEN", {});
    delivery.enqueue("EXIT", {});
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(scheduler.timers[0]?.delayMs).toBe(250);

    scheduler.run();
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(scheduler.timers[0]?.delayMs).toBe(500);

    scheduler.run();
    await flush();
    // OPEN succeeds on the third attempt, then EXIT is drained immediately.
    expect(fetchMock).toHaveBeenCalledTimes(4);
    const events = fetchMock.mock.calls.map(
      (call) => JSON.parse((call[1] as RequestInit).body as string).event,
    );
    expect(events).toEqual(["OPEN", "OPEN", "OPEN", "EXIT"]);
    expect(delivery.pending).toBe(0);
  });

  it("caps backoff and drops an event after the max attempts, notifying the caller", async () => {
    const scheduler = makeScheduler();
    fetchMock.mockRejectedValue(new Error("permafail"));
    const onDeliveryFailed = vi.fn();

    const delivery = new EventDelivery({
      linkToken: "tok",
      serverUrl: "https://api.plaidify.test",
      fetchImpl: fetchMock as unknown as typeof fetch,
      setTimer: scheduler.setTimer,
      clearTimer: scheduler.clearTimer,
      maxAttempts: 6,
      baseDelayMs: 250,
      maxDelayMs: 4000,
      onDeliveryFailed,
    });

    delivery.enqueue("COMPLETE", { status: "ok" });

    const expectedDelays = [250, 500, 1000, 2000, 4000];
    await flush();
    for (const expected of expectedDelays) {
      expect(scheduler.timers[0]?.delayMs).toBe(expected);
      scheduler.run();
      await flush();
    }

    expect(fetchMock).toHaveBeenCalledTimes(6);
    expect(delivery.pending).toBe(0);
    expect(onDeliveryFailed).toHaveBeenCalledTimes(1);
    const [eventName, error] = onDeliveryFailed.mock.calls[0] as [
      string,
      Error,
    ];
    expect(eventName).toBe("COMPLETE");
    expect(error.message).toBe("permafail");
  });

  it("treats a non-2xx response as a retry", async () => {
    const scheduler = makeScheduler();
    fetchMock
      .mockResolvedValueOnce(new Response(null, { status: 500 }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }));

    const delivery = new EventDelivery({
      linkToken: "tok",
      serverUrl: "https://api.plaidify.test",
      fetchImpl: fetchMock as unknown as typeof fetch,
      setTimer: scheduler.setTimer,
      clearTimer: scheduler.clearTimer,
    });

    delivery.enqueue("EXIT", {});
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    scheduler.run();
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(delivery.pending).toBe(0);
  });

  it("rejects construction without a token or server url", () => {
    expect(
      () =>
        new EventDelivery({
          linkToken: "",
          serverUrl: "https://api",
          fetchImpl: fetchMock as unknown as typeof fetch,
        }),
    ).toThrow(/linkToken/);
    expect(
      () =>
        new EventDelivery({
          linkToken: "tok",
          serverUrl: "",
          fetchImpl: fetchMock as unknown as typeof fetch,
        }),
    ).toThrow(/serverUrl/);
  });
});

describe("postBridgeEvent", () => {
  it("posts to the parent frame when embedded", () => {
    const targetWindow = {
      postMessage: vi.fn(),
    } as unknown as Window;

    postBridgeEvent(
      "OPEN",
      { link_session_id: "ls_1" },
      {
        parentOrigin: "https://merchant.example",
        inIframe: true,
        targetWindow,
      },
    );

    expect(targetWindow.postMessage).toHaveBeenCalledWith(
      {
        source: "plaidify-link",
        event: "OPEN",
        link_session_id: "ls_1",
      },
      "https://merchant.example",
    );
  });

  it("serializes to a React Native bridge when present", () => {
    const reactNativeBridge = { postMessage: vi.fn() };

    postBridgeEvent(
      "EXIT",
      { reason: "user-closed" },
      {
        parentOrigin: "*",
        inIframe: false,
        reactNativeBridge,
      },
    );

    expect(reactNativeBridge.postMessage).toHaveBeenCalledTimes(1);
    expect(JSON.parse(reactNativeBridge.postMessage.mock.calls[0][0])).toEqual({
      source: "plaidify-link",
      event: "EXIT",
      reason: "user-closed",
    });
  });

  it("delivers the raw object to a WKWebView bridge", () => {
    const webkitBridge = { postMessage: vi.fn() };

    postBridgeEvent(
      "CONNECTED",
      { public_token: "public-1" },
      {
        parentOrigin: "*",
        inIframe: false,
        webkitBridge,
      },
    );

    expect(webkitBridge.postMessage).toHaveBeenCalledTimes(1);
    expect(webkitBridge.postMessage.mock.calls[0][0]).toEqual({
      source: "plaidify-link",
      event: "CONNECTED",
      public_token: "public-1",
    });
  });

  it("is a no-op when neither transport is available", () => {
    expect(() =>
      postBridgeEvent(
        "OPEN",
        {},
        {
          parentOrigin: "https://merchant.example",
          inIframe: false,
        },
      ),
    ).not.toThrow();
  });
});
