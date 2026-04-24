/**
 * Durable event delivery for hosted-link lifecycle events.
 *
 * This is the typed port of the `eventDelivery` retry queue that lives
 * in the legacy `frontend/link-page.js`. Hosted-link lifecycle events
 * (OPEN, EXIT, MFA_REQUIRED, COMPLETE, ERROR, etc.) must reach the
 * server so session state, SSE, and webhooks stay in sync with what
 * actually happened in the browser. Posts are queued and retried with
 * exponential backoff; exhausted events surface to the parent through
 * the `postEvent` bridge so operators can detect drift.
 *
 * The defaults mirror the legacy implementation (max 6 attempts, base
 * 250 ms, cap 4 000 ms) so behaviour is preserved when #68 flips the
 * React bundle on via `HOSTED_LINK_FRONTEND=react`.
 */

export interface EventDeliveryOptions {
  readonly linkToken: string;
  readonly serverUrl: string;
  readonly maxAttempts?: number;
  readonly baseDelayMs?: number;
  readonly maxDelayMs?: number;
  readonly fetchImpl?: typeof fetch;
  readonly setTimer?: (cb: () => void, delayMs: number) => unknown;
  readonly clearTimer?: (handle: unknown) => void;
  readonly onDeliveryFailed?: (event: string, error: Error) => void;
}

interface QueuedEvent {
  readonly event: string;
  readonly payload: Record<string, unknown>;
  attempts: number;
}

const DEFAULT_MAX_ATTEMPTS = 6;
const DEFAULT_BASE_DELAY_MS = 250;
const DEFAULT_MAX_DELAY_MS = 4000;

export class EventDelivery {
  private readonly queue: QueuedEvent[] = [];
  private inFlight = false;
  private retryTimer: unknown = null;
  private readonly options: Required<
    Omit<EventDeliveryOptions, "fetchImpl" | "setTimer" | "clearTimer" | "onDeliveryFailed">
  > & {
    readonly fetchImpl: typeof fetch;
    readonly setTimer: (cb: () => void, delayMs: number) => unknown;
    readonly clearTimer: (handle: unknown) => void;
    readonly onDeliveryFailed?: (event: string, error: Error) => void;
  };

  constructor(options: EventDeliveryOptions) {
    if (!options.linkToken) {
      throw new Error("EventDelivery requires a linkToken.");
    }
    if (!options.serverUrl) {
      throw new Error("EventDelivery requires a serverUrl.");
    }
    this.options = {
      linkToken: options.linkToken,
      serverUrl: options.serverUrl.replace(/\/$/, ""),
      maxAttempts: options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS,
      baseDelayMs: options.baseDelayMs ?? DEFAULT_BASE_DELAY_MS,
      maxDelayMs: options.maxDelayMs ?? DEFAULT_MAX_DELAY_MS,
      fetchImpl: options.fetchImpl ?? globalThis.fetch.bind(globalThis),
      setTimer:
        options.setTimer ??
        ((cb, delayMs) => globalThis.setTimeout(cb, delayMs) as unknown),
      clearTimer:
        options.clearTimer ??
        ((handle) => {
          if (handle !== null && handle !== undefined) {
            globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>);
          }
        }),
      onDeliveryFailed: options.onDeliveryFailed,
    };
  }

  /** Enqueue an event and try to deliver it immediately. */
  enqueue(event: string, payload: Record<string, unknown> = {}): void {
    this.queue.push({ event, payload, attempts: 0 });
    void this.drain();
  }

  /** Number of events still waiting to be delivered. */
  get pending(): number {
    return this.queue.length;
  }

  /** True while a fetch call is outstanding. */
  get busy(): boolean {
    return this.inFlight;
  }

  private scheduleRetry(delayMs: number): void {
    if (this.retryTimer !== null) {
      return;
    }
    this.retryTimer = this.options.setTimer(() => {
      this.retryTimer = null;
      void this.drain();
    }, delayMs);
  }

  async drain(): Promise<void> {
    if (this.inFlight) {
      return;
    }
    const next = this.queue[0];
    if (!next) {
      return;
    }
    this.inFlight = true;
    const url = `${this.options.serverUrl}/link/sessions/${encodeURIComponent(
      this.options.linkToken,
    )}/event`;
    try {
      const response = await this.options.fetchImpl(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ event: next.event, ...next.payload }),
      });
      if (!response.ok) {
        throw new Error(`event delivery failed with status ${response.status}`);
      }
      this.queue.shift();
      this.inFlight = false;
      if (this.queue.length > 0) {
        void this.drain();
      }
    } catch (err) {
      this.inFlight = false;
      next.attempts += 1;
      if (next.attempts >= this.options.maxAttempts) {
        this.queue.shift();
        const error = err instanceof Error ? err : new Error(String(err));
        try {
          this.options.onDeliveryFailed?.(next.event, error);
        } catch {
          // best-effort telemetry only
        }
        if (this.queue.length > 0) {
          void this.drain();
        }
        return;
      }
      const backoff = Math.min(
        this.options.baseDelayMs * 2 ** (next.attempts - 1),
        this.options.maxDelayMs,
      );
      this.scheduleRetry(backoff);
    }
  }

  /** Abandon any pending work and clear timers. */
  dispose(): void {
    if (this.retryTimer !== null) {
      this.options.clearTimer(this.retryTimer);
      this.retryTimer = null;
    }
    this.queue.length = 0;
  }
}

/**
 * Fire-and-forget bridge notification to the parent frame and/or the
 * native webview shell. Matches the message shape the legacy page uses
 * so the Plaidify SDKs (JS, Swift) keep working unchanged:
 *   - React Native WebView receives the JSON-serialised string.
 *   - WKWebView (webkit.messageHandlers.plaidifyLink) receives the
 *     object directly, which is what the iOS SDK expects.
 */
export interface ParentBridgeOptions {
  readonly parentOrigin: string;
  readonly inIframe: boolean;
  readonly targetWindow?: Window | null;
  readonly reactNativeBridge?: { postMessage: (payload: string) => void } | null;
  readonly webkitBridge?:
    | { postMessage: (payload: Record<string, unknown>) => void }
    | null;
  /** Legacy alias retained for tests that pre-date the split bridges. */
  readonly nativeBridge?: { postMessage: (payload: string) => void } | null;
}

export function postBridgeEvent(
  event: string,
  payload: Record<string, unknown>,
  options: ParentBridgeOptions,
): void {
  const message = { source: "plaidify-link", event, ...payload };

  if (options.inIframe && options.targetWindow) {
    try {
      options.targetWindow.postMessage(message, options.parentOrigin);
    } catch {
      // Ignore bridge delivery failures; server-side delivery is the
      // source of truth for lifecycle events.
    }
  }

  const rn = options.reactNativeBridge ?? options.nativeBridge ?? null;
  if (rn) {
    try {
      rn.postMessage(JSON.stringify(message));
    } catch {
      // Same rationale as above.
    }
  }

  if (options.webkitBridge) {
    try {
      options.webkitBridge.postMessage(message);
    } catch {
      // Same rationale as above.
    }
  }
}
