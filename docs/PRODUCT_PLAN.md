# Product Plan

Plaidify is being developed as a production service for authenticated web access. It has not been released yet; [CHANGELOG.md](../CHANGELOG.md) records what exists.

## Near-Term Priorities

### Service Hardening

- Tighten hosted link security and launch flows
- Maintain strict read-only runtime defaults
- Keep production deployment guidance aligned with the actual runtime model
- Keep internal fixtures isolated from public discovery surfaces

### Integration Reliability

- Improve hosted link bootstrapping for web and mobile clients
- Improve access job durability, recovery, and observability
- Strengthen MFA continuation and retry behavior
- Improve connector validation and registry workflows

### Operator Experience

- Expand health, audit, and diagnostics coverage
- Improve deployment ergonomics for production infrastructure
- Continue tightening API key, agent, and scope enforcement
- Keep operational documentation concise and accurate

### Product Surface

- Keep SDKs aligned with the production API contract, and publish them
- Keep public docs focused on deployment and integration
- Keep the bundled sandbox (demo sites behind `DEMO_MODE`) for evaluation and tests only
- Preserve only neutral internal fixtures for automated validation

## Deliberate Non-Goals

- Hosting public showcase portals: the bundled demo sites (`src/demo/`, `scripts/demo.py`) run locally, for evaluation and tests
- Shipping application examples as part of the core product repo
- Treating internal fixture connectors as customer-facing integrations

## Success Criteria

- Public docs describe deployment and integration clearly
- Hosted link flows are production-safe by default
- Internal fixtures remain available for automated validation without surfacing as product integrations
- SDKs and tests validate the same contract the service exposes
