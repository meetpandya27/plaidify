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
 * The defaults (max 6 attempts, base 250 ms, cap 4 000 ms) are those of
 * the page this bundle replaced, so host apps saw no change in delivery.
 */

import { normalizeOrigin } from "./config";

/**
 * Sends one event while the page is being torn down. Returns false when
 * the browser refused to queue it.
 */
export type TeardownSender = (url: string, body: string) => boolean;

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
  /** Overrides the sendBeacon / keepalive transport used at teardown (tests). */
  readonly sendOnTeardown?: TeardownSender;
}

interface QueuedEvent {
  readonly event: string;
  readonly payload: Record<string, unknown>;
  attempts: number;
}

const DEFAULT_MAX_ATTEMPTS = 6;
const DEFAULT_BASE_DELAY_MS = 250;
const DEFAULT_MAX_DELAY_MS = 4000;

/**
 * The wire body for one event. The envelope is written last so a payload
 * field can never replace the event name (TELEMETRY payloads used to carry
 * their own `event` key and were filed under it).
 */
export function eventBody(event: string, payload: Record<string, unknown>): string {
  return JSON.stringify({ ...payload, event });
}

/**
 * Default teardown transport: sendBeacon, falling back to a keepalive
 * fetch. `text/plain` keeps the request CORS-simple (no preflight to be
 * cancelled mid-unload); the server parses the body as JSON regardless.
 */
export function defaultTeardownSender(url: string, body: string): boolean {
  const nav = typeof navigator !== "undefined" ? navigator : undefined;
  if (nav && typeof nav.sendBeacon === "function") {
    try {
      if (nav.sendBeacon(url, new Blob([body], { type: "text/plain;charset=UTF-8" }))) {
        return true;
      }
    } catch {
      // Fall through to fetch.
    }
  }
  if (typeof fetch === "function") {
    try {
      void fetch(url, {
        method: "POST",
        body,
        keepalive: true,
        headers: { "Content-Type": "text/plain;charset=UTF-8" },
      }).catch(() => undefined);
      return true;
    } catch {
      return false;
    }
  }
  return false;
}

export class EventDelivery {
  private readonly queue: QueuedEvent[] = [];
  private inFlight = false;
  private retryTimer: unknown = null;
  private readonly options: Required<
    Omit<
      EventDeliveryOptions,
      "fetchImpl" | "setTimer" | "clearTimer" | "onDeliveryFailed" | "sendOnTeardown"
    >
  > & {
    readonly fetchImpl: typeof fetch;
    readonly setTimer: (cb: () => void, delayMs: number) => unknown;
    readonly clearTimer: (handle: unknown) => void;
    readonly onDeliveryFailed?: (event: string, error: Error) => void;
    readonly sendOnTeardown: TeardownSender;
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
      sendOnTeardown: options.sendOnTeardown ?? defaultTeardownSender,
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

  private get eventUrl(): string {
    return `${this.options.serverUrl}/link/sessions/${encodeURIComponent(
      this.options.linkToken,
    )}/event`;
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
    try {
      const response = await this.options.fetchImpl(this.eventUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: eventBody(next.event, next.payload),
        // A post that is already on the wire when the page is torn down
        // still reaches the server.
        keepalive: true,
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

  /**
   * The page is going away: hand every event that has not started its
   * trip — plus `finalEvents`, typically EXIT — to the beacon transport,
   * which outlives the document. The post already in flight is left alone;
   * it was sent with keepalive.
   */
  flushOnTeardown(
    finalEvents: readonly { event: string; payload?: Record<string, unknown> }[] = [],
  ): void {
    if (this.retryTimer !== null) {
      this.options.clearTimer(this.retryTimer);
      this.retryTimer = null;
    }
    const waiting = this.inFlight ? this.queue.slice(1) : this.queue.slice();
    this.queue.length = 0;
    for (const item of [...waiting, ...finalEvents]) {
      try {
        this.options.sendOnTeardown(this.eventUrl, eventBody(item.event, item.payload ?? {}));
      } catch {
        // Nothing else can be done while the page unloads.
      }
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
 * The embedding window, reached only through origins the session allows.
 *
 * `?origin=` and the referrer are attacker-controllable, so they only pick
 * among origins the server lists in the session's `allowed_origins` (plus
 * the page's own origin, which `frame-ancestors 'self'` always admits).
 * Messages wait in a small buffer until the session status says which
 * origins those are, and "*" is never a target.
 */
export interface ParentChannelOptions {
  readonly targetWindow: Pick<Window, "postMessage"> | null;
  readonly ownOrigin: string;
  readonly candidateOrigin: string | null;
  readonly maxBuffered?: number;
}

const DEFAULT_MAX_BUFFERED = 50;

export class ParentChannel {
  private targets: readonly string[] | null = null;
  private readonly buffer: Record<string, unknown>[] = [];

  constructor(private readonly options: ParentChannelOptions) {}

  /** Origins messages go to; null until the session status arrived. */
  get resolvedTargets(): readonly string[] | null {
    return this.targets;
  }

  post(message: Record<string, unknown>): void {
    if (!this.options.targetWindow) {
      return;
    }
    if (this.targets === null) {
      if (this.buffer.length < (this.options.maxBuffered ?? DEFAULT_MAX_BUFFERED)) {
        this.buffer.push(message);
      }
      return;
    }
    this.deliver(message);
  }

  /**
   * Fix the target origins from the session status. `allowedOrigins` is
   * undefined when the server predates the field; an empty list means only
   * the page's own origin may embed it.
   */
  resolve(allowedOrigins: readonly string[] | null | undefined): void {
    this.targets = resolveParentTargets({
      candidateOrigin: this.options.candidateOrigin,
      ownOrigin: this.options.ownOrigin,
      allowedOrigins,
    });
    const pending = this.buffer.splice(0, this.buffer.length);
    for (const message of pending) {
      this.deliver(message);
    }
  }

  private deliver(message: Record<string, unknown>): void {
    const target = this.options.targetWindow;
    if (!target) {
      return;
    }
    for (const origin of this.targets ?? []) {
      try {
        // The browser drops the message unless the parent really is
        // `origin`, so offering it to each allowed origin reaches only
        // the one actually embedding us.
        target.postMessage(message, origin);
      } catch {
        // Ignore bridge delivery failures; server-side delivery is the
        // source of truth for lifecycle events.
      }
    }
  }
}

export function resolveParentTargets(input: {
  readonly candidateOrigin: string | null;
  readonly ownOrigin: string;
  readonly allowedOrigins: readonly string[] | null | undefined;
}): string[] {
  const candidate = normalizeOrigin(input.candidateOrigin);
  if (input.allowedOrigins === undefined || input.allowedOrigins === null) {
    return candidate ? [candidate] : [];
  }
  const allowed: string[] = [];
  for (const entry of [...input.allowedOrigins, input.ownOrigin]) {
    const origin = normalizeOrigin(entry);
    if (origin && !allowed.includes(origin)) {
      allowed.push(origin);
    }
  }
  if (candidate && allowed.includes(candidate)) {
    return [candidate];
  }
  return allowed;
}

/**
 * Fire-and-forget bridge notification to the parent frame and/or the
 * native webview shell. Matches the message shape the legacy page uses
 * so the Plaidify SDKs (JS, Swift, Android) keep working unchanged:
 *   - React Native WebView receives the JSON-serialised string.
 *   - WKWebView (webkit.messageHandlers.plaidifyLink) receives the
 *     object directly, which is what the iOS SDK expects.
 *   - Android (window.plaidifyLink) receives the JSON-serialised string.
 */
export interface ParentBridgeOptions {
  readonly parent?: Pick<ParentChannel, "post"> | null;
  readonly reactNativeBridge?: { postMessage: (payload: string) => void } | null;
  readonly webkitBridge?:
    | { postMessage: (payload: Record<string, unknown>) => void }
    | null;
  readonly androidBridge?: { postMessage: (payload: string) => void } | null;
}

export function postBridgeEvent(
  event: string,
  payload: Record<string, unknown>,
  options: ParentBridgeOptions,
): void {
  // Envelope last: a payload key must not rename the event or its source.
  const message = { ...payload, source: "plaidify-link", event };

  if (options.parent) {
    try {
      options.parent.post(message);
    } catch {
      // Ignore bridge delivery failures; server-side delivery is the
      // source of truth for lifecycle events.
    }
  }

  const serialized = JSON.stringify(message);
  for (const bridge of [options.reactNativeBridge, options.androidBridge]) {
    if (!bridge) continue;
    try {
      bridge.postMessage(serialized);
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
