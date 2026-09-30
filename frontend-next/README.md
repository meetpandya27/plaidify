# @plaidify/hosted-link-frontend

The Plaidify hosted Link page: React + Vite + TypeScript. FastAPI serves the
built `dist/index.html` at `GET /link` (and the bundle's assets under
`/ui-next`), so the page must be built before `/link` works — the Docker image
and CI build it; locally run `npm run build`. It replaced the legacy static
page in April 2026 (epic [#47](https://github.com/meetpandya27/plaidify/issues/47)).

## Local development

```bash
cd frontend-next
npm ci
npm run typecheck
npm test
npm run build
```

The Vite dev server (`npm run dev`) proxies `/link/sessions` and
`/organizations` to `http://127.0.0.1:8000` so it can run against a
local Plaidify API.

## URL parameters

The page is opened as `/link?token=<link_token>&...`:

| Parameter | Meaning |
|-----------|---------|
| `token` | The link session. Exactly one; a URL with two `token` parameters is refused as invalid. |
| `origin` | Hint for the embedding page's origin. Events only ever go to origins the session allows (`allowed_origins` from the session status, plus the page's own origin) — never `*`. |
| `theme` | `light` or `dark` (default: the system setting). |
| `locale` | `en-US`, `en-CA` or `fr-CA`. |
| `accent`, `bg` | Hex colours (`#0b8f73`) for buttons/focus and the page background. |
| `radius` | Card corner radius, e.g. `24px` or `1.5rem`. |
| `logo` | A `data:image/…;base64,` logo of at most 32 KB (the page's CSP loads no remote images). |

Values that do not match these forms are ignored. `server` (API base URL)
is honoured by the Vite dev server only; a built page talks to the origin
that served it.

## Events

Every lifecycle event goes to the server (`POST /link/sessions/{token}/event`,
retried with backoff) and to whichever host transport is present, as
`{source: "plaidify-link", event, ...}`:

- the embedding window, via `postMessage` to an allowed origin;
- React Native: `window.ReactNativeWebView.postMessage(json)`;
- iOS: `window.webkit.messageHandlers.plaidifyLink.postMessage(object)`;
- Android: `window.plaidifyLink.postMessage(json)`.

`EXIT` is sent only when the user leaves from the error screen or the page
is torn down (`pagehide`, delivered by `sendBeacon`); an `ERROR` leaves the
user on a retry screen. UX telemetry travels as `event: "TELEMETRY"` with
the telemetry event in `name`. The public token is only ever in the
`CONNECTED` event — it is not shown on the page.
