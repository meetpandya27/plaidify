import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  EventDelivery,
  ParentChannel,
  eventBody,
  postBridgeEvent,
  resolveParentTargets,
} from "./events";

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
    // Survives the page being torn down mid-request.
    expect(init.keepalive).toBe(true);
  });

  it("keeps the envelope event name even when the payload has an `event` key", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    const delivery = new EventDelivery({
      linkToken: "tok",
      serverUrl: "https://api.plaidify.test",
      fetchImpl: fetchMock as unknown as typeof fetch,
    });

    delivery.enqueue("TELEMETRY", { event: "step_view", name: "step_view", step: "select" });
    await flush();

    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(body).toEqual({ event: "TELEMETRY", name: "step_view", step: "select" });
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

describe("EventDelivery.flushOnTeardown", () => {
  it("beacons queued events plus the final ones and empties the queue", async () => {
    const scheduler = makeScheduler();
    const beacons: Array<{ url: string; body: unknown }> = [];
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    const delivery = new EventDelivery({
      linkToken: "tok",
      serverUrl: "https://api.plaidify.test",
      fetchImpl: fetchMock as unknown as typeof fetch,
      setTimer: scheduler.setTimer,
      clearTimer: scheduler.clearTimer,
      sendOnTeardown: (url, body) => {
        beacons.push({ url, body: JSON.parse(body) });
        return true;
      },
    });

    delivery.enqueue("ERROR", { error_code: "network_error" });
    await flush();
    // The first attempt failed; ERROR now waits for its retry timer.
    expect(delivery.busy).toBe(false);
    expect(delivery.pending).toBe(1);

    delivery.flushOnTeardown([{ event: "EXIT", payload: { reason: "page_closed" } }]);

    expect(beacons).toEqual([
      {
        url: "https://api.plaidify.test/link/sessions/tok/event",
        body: { event: "ERROR", error_code: "network_error" },
      },
      {
        url: "https://api.plaidify.test/link/sessions/tok/event",
        body: { event: "EXIT", reason: "page_closed" },
      },
    ]);
    expect(delivery.pending).toBe(0);
  });

  it("leaves the post already on the wire to its keepalive request", async () => {
    const beacons: string[] = [];
    let release: (value: Response) => void = () => undefined;
    const fetchMock = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        release = resolve;
      }),
    );
    const delivery = new EventDelivery({
      linkToken: "tok",
      serverUrl: "https://api.plaidify.test",
      fetchImpl: fetchMock as unknown as typeof fetch,
      sendOnTeardown: (_url, body) => {
        beacons.push(JSON.parse(body).event);
        return true;
      },
    });

    delivery.enqueue("OPEN", {});
    delivery.enqueue("INSTITUTION_SELECTED", { site: "hydro_one" });
    expect(delivery.busy).toBe(true);

    delivery.flushOnTeardown([{ event: "EXIT" }]);
    expect(beacons).toEqual(["INSTITUTION_SELECTED", "EXIT"]);
    release(new Response(null, { status: 204 }));
  });
});

describe("eventBody", () => {
  it("writes the envelope after the payload", () => {
    expect(JSON.parse(eventBody("EXIT", { event: "spoof", reason: "user_exit" }))).toEqual({
      event: "EXIT",
      reason: "user_exit",
    });
  });
});

describe("resolveParentTargets", () => {
  it("uses the candidate when the session allows it", () => {
    expect(
      resolveParentTargets({
        candidateOrigin: "https://merchant.example",
        ownOrigin: "https://api.plaidify.test",
        allowedOrigins: ["https://merchant.example", "https://other.example"],
      }),
    ).toEqual(["https://merchant.example"]);
  });

  it("ignores a candidate the session does not allow", () => {
    expect(
      resolveParentTargets({
        candidateOrigin: "https://attacker.example",
        ownOrigin: "https://api.plaidify.test",
        allowedOrigins: ["https://merchant.example/"],
      }),
    ).toEqual(["https://merchant.example", "https://api.plaidify.test"]);
  });

  it("admits only the page's own origin when the list is empty", () => {
    expect(
      resolveParentTargets({
        candidateOrigin: "https://attacker.example",
        ownOrigin: "https://api.plaidify.test",
        allowedOrigins: [],
      }),
    ).toEqual(["https://api.plaidify.test"]);
  });

  it("falls back to the candidate for servers without allowed_origins, never '*'", () => {
    expect(
      resolveParentTargets({
        candidateOrigin: "https://merchant.example",
        ownOrigin: "https://api.plaidify.test",
        allowedOrigins: undefined,
      }),
    ).toEqual(["https://merchant.example"]);
    expect(
      resolveParentTargets({
        candidateOrigin: "*",
        ownOrigin: "https://api.plaidify.test",
        allowedOrigins: undefined,
      }),
    ).toEqual([]);
  });
});

describe("ParentChannel", () => {
  it("holds messages until the session names the allowed origins", () => {
    const targetWindow = { postMessage: vi.fn() };
    const channel = new ParentChannel({
      targetWindow,
      ownOrigin: "https://api.plaidify.test",
      candidateOrigin: "https://attacker.example",
    });

    channel.post({ source: "plaidify-link", event: "OPEN" });
    expect(targetWindow.postMessage).not.toHaveBeenCalled();

    channel.resolve(["https://merchant.example"]);
    channel.post({ source: "plaidify-link", event: "CONNECTED", public_token: "public-1" });

    const targets = targetWindow.postMessage.mock.calls.map((call) => call[1]);
    expect(new Set(targets)).toEqual(
      new Set(["https://merchant.example", "https://api.plaidify.test"]),
    );
    expect(targets).not.toContain("*");
    expect(targets).not.toContain("https://attacker.example");
    expect(targetWindow.postMessage).toHaveBeenCalledWith(
      { source: "plaidify-link", event: "OPEN" },
      "https://merchant.example",
    );
  });

  it("is a no-op without a parent window", () => {
    const channel = new ParentChannel({
      targetWindow: null,
      ownOrigin: "https://api.plaidify.test",
      candidateOrigin: null,
    });
    expect(() => {
      channel.post({ event: "OPEN" });
      channel.resolve([]);
    }).not.toThrow();
  });
});

describe("postBridgeEvent", () => {
  it("hands the message to the parent channel", () => {
    const parent = { post: vi.fn() };

    postBridgeEvent("OPEN", { link_session_id: "ls_1" }, { parent });

    expect(parent.post).toHaveBeenCalledWith({
      source: "plaidify-link",
      event: "OPEN",
      link_session_id: "ls_1",
    });
  });

  it("does not let a payload rename the event or its source", () => {
    const parent = { post: vi.fn() };

    postBridgeEvent(
      "TELEMETRY",
      { event: "step_view", source: "evil", name: "step_view" },
      { parent },
    );

    expect(parent.post).toHaveBeenCalledWith({
      source: "plaidify-link",
      event: "TELEMETRY",
      name: "step_view",
    });
  });

  it("serializes to a React Native bridge when present", () => {
    const reactNativeBridge = { postMessage: vi.fn() };

    postBridgeEvent("EXIT", { reason: "user-closed" }, { reactNativeBridge });

    expect(reactNativeBridge.postMessage).toHaveBeenCalledTimes(1);
    expect(JSON.parse(reactNativeBridge.postMessage.mock.calls[0][0])).toEqual({
      source: "plaidify-link",
      event: "EXIT",
      reason: "user-closed",
    });
  });

  it("serializes to the Android bridge when present", () => {
    const androidBridge = { postMessage: vi.fn() };

    postBridgeEvent("CONNECTED", { public_token: "public-1" }, { androidBridge });

    expect(JSON.parse(androidBridge.postMessage.mock.calls[0][0])).toEqual({
      source: "plaidify-link",
      event: "CONNECTED",
      public_token: "public-1",
    });
  });

  it("delivers the raw object to a WKWebView bridge", () => {
    const webkitBridge = { postMessage: vi.fn() };

    postBridgeEvent("CONNECTED", { public_token: "public-1" }, { webkitBridge });

    expect(webkitBridge.postMessage).toHaveBeenCalledTimes(1);
    expect(webkitBridge.postMessage.mock.calls[0][0]).toEqual({
      source: "plaidify-link",
      event: "CONNECTED",
      public_token: "public-1",
    });
  });

  it("is a no-op when no transport is available", () => {
    expect(() => postBridgeEvent("OPEN", {}, {})).not.toThrow();
  });
});
