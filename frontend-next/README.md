# @plaidify/hosted-link-frontend

React + Vite + TypeScript rewrite of the Plaidify hosted Link page.

This package is under active construction. It is not yet wired into the
FastAPI service — the legacy static page in `frontend/link.html`
continues to serve `/link` traffic. The rewrite is tracked by epic
[#47](https://github.com/meetpandya27/plaidify/issues/47) with phased
sub-tasks:

- [#66](https://github.com/meetpandya27/plaidify/issues/66) scaffold
  (this package)
- [#67](https://github.com/meetpandya27/plaidify/issues/67) port the
  state machine and event bridge into typed React modules
- [#68](https://github.com/meetpandya27/plaidify/issues/68) serve the
  built bundle from FastAPI behind `HOSTED_LINK_FRONTEND=react`
- [#65](https://github.com/meetpandya27/plaidify/issues/65) flip the
  default to React and retire the legacy HTML

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
