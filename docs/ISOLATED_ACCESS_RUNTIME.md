# Plaidify Isolated Access Runtime

This document defines the execution model Plaidify should move toward for safer authenticated website access, better multi-tenant isolation, and future AI-agent support.

## Why This Exists

Plaidify already isolates browser automation at the Playwright `BrowserContext` level. That is a good baseline, but it is not the final isolation boundary for a multi-user, agent-driven platform.

The next step is to treat every website access flow as an **access job** with its own isolated executor. That reduces the chance of session bleed, cookie reuse, temporary file collisions, concurrent write conflicts, and cross-user data mangling.

## Terms

- **Access request**: An API-level request to read or act on a user's authenticated website data.
- **Access job**: A durable unit of work derived from an access request. Includes `job_id`, `user_id`, `site`, `scopes`, requested action, TTL, and audit context.
- **Executor**: The runtime that performs the website access job. It owns the browser session, temporary files, credentials, and extraction output for the lifetime of the job.
- **Control plane**: API, auth, consent, queueing, auditing, and result orchestration.
- **Data plane**: The isolated executor that logs into the site and performs browser work.

## Design Goal

Every user access flow should run with a clear boundary:

1. Credentials are only exposed to the executor for the active job.
2. Cookies, browser storage, downloads, screenshots, traces, and artifacts are job-scoped.
3. Results are returned as structured output or approved artifacts.
4. The executor is destroyed after the job ends or expires.

## Current Baseline

Today Plaidify already has useful foundations:

- Each session gets its own Playwright `BrowserContext`.
- Link sessions and event delivery are keyed separately in Redis-backed stores.
- Production requires Redis rather than silently falling back to process-local state.

That is strong session isolation, but it still shares the wider service process, filesystem, and worker runtime.

## Recommended Isolation Model

The long-term model is **one isolated executor per access job**, not one giant shared browser worker for every user.

### Control Plane Responsibilities

The main Plaidify API should handle:

- Authentication and user identity
- Consent and scope enforcement
- Job creation and queueing
- Access policy checks
- Audit logs and job status
- Result retrieval and artifact references

The control plane should not keep raw credentials, long-lived browser state, or job-local temp data longer than necessary.

### Executor Responsibilities

Each executor should own:

- Decrypted credentials for the active job only
- Dedicated Playwright browser context
- Dedicated temporary directory
- Dedicated download directory
- Dedicated trace / screenshot storage path
- Per-job environment variables and runtime settings

When the job finishes, all of that state should be cleaned up.

## Isolation Levels

### Level 0: BrowserContext Isolation

This is the current baseline.

Characteristics:

- Separate Playwright browser context per session
- Shared API process and shared container
- Shared local filesystem unless explicitly segmented

This is acceptable for local development and early single-tenant deployments.

### Level 1: Job-Scoped Executor Process

Recommended near-term default.

Characteristics:

- Separate process for each access job
- Separate browser context per job
- Separate temp/download/artifact directories per job
- Explicit cleanup after completion
- Safer memory and filesystem boundaries than a single long-lived worker process

This is a good Docker-first path because it improves safety without requiring a cluster.

### Level 2: Ephemeral Container or Pod Per Job

Recommended for multi-tenant and agent-heavy production.

Characteristics:

- One short-lived container or pod per access job
- Job-scoped identity, scratch space, and network policy
- Strong cleanup guarantees after pod termination
- Better blast-radius control for untrusted website automation

This is the right model once Plaidify is running many concurrent users or autonomous agents.

## Required Safety Rules

Regardless of isolation level, these rules should hold:

1. No shared cookie jar or Playwright user data directory across jobs.
2. No shared download or temp path across jobs.
3. No reuse of decrypted credentials after the job completes.
4. No direct LLM or agent access to stored credentials.
5. Read and write operations must use explicit scope and consent checks.
6. Website actions that mutate user state should use locking.

## Concurrency Rules

To prevent user data from getting mangled, Plaidify should add job-level locking.

Recommended policy:

- Allow multiple read jobs for a user only when they target different sites.
- Allow at most one active write job per `user_id + site`.
- Block or queue a second write job until the first one completes.
- Optionally block overlapping read and write jobs on the same site if the site is stateful or fragile.

That is more important than browser isolation alone. Isolation helps, but locking prevents logical conflicts.

## Agent-Safe Runtime Pattern

For future AI agents, the preferred pattern is:

1. The agent requests data or an action.
2. Plaidify checks consent, scopes, and policy.
3. Plaidify creates an access job.
4. An isolated executor performs the login and extraction.
5. The agent receives structured output, not raw credentials or browser control.

This keeps the agent in a planning role and keeps the executor in the privileged execution role.

### What The Agent Should Not Get

- Raw username and password material
- Long-lived session cookies
- Direct shell access to the executor
- Arbitrary browser control without a policy boundary

### What The Agent Can Get

- Structured extracted data
- Job status events
- Approved artifacts such as statement PDFs or screenshots
- Narrowly scoped action results

## Docker-First Recommendation

For developer adoption, the simplest path is:

- Keep a single Plaidify control-plane service
- Add a dedicated executor service or worker process for access jobs
- Give each job its own temp, download, and trace directories
- Run executor concurrency at a controlled limit

This preserves an easy `docker compose up` experience while making the runtime more predictable and safer.

Recommended Docker progression:

1. Start with job-scoped process isolation inside an executor service.
2. Add queue-based dispatch from API to executor.
3. Keep Redis as the coordination layer for job state and locks.

This is better than forcing Kubernetes on every developer.

## Kubernetes-Later Recommendation

Once Plaidify needs stronger tenant isolation, move the executor to Kubernetes jobs or scaled job workers.

Recommended shape:

- API control plane as a Deployment
- Redis and Postgres as managed services or dedicated charts
- Access jobs executed in ephemeral pods
- `emptyDir` scratch space for temp files and browser data
- Secret injection only for the job lifetime
- NetworkPolicy limiting pod egress and ingress as much as practical

Good Kubernetes fits:

- Multi-tenant SaaS
- Strict isolation requirements
- Heavy agent-driven automation
- Burst workloads that benefit from queue-backed scaling

## Suggested Rollout Phases

### Phase 1: Formalize Access Jobs

- Introduce an `AccessJob` model and status lifecycle
- Add job IDs, TTLs, and audit linkage
- Add per `user_id + site` locking for writes

### Phase 2: Split Control Plane From Executor

- Move browser automation behind an executor boundary
- Give each job dedicated temp/download directories
- Stop treating the API process as the browser runtime

### Phase 3: Docker Distribution

- Add a Compose profile for control plane + executor runtime
- Document local isolation mode for developers

### Phase 4: Kubernetes Distribution

- Add Helm support for control plane plus ephemeral executors
- Add a pod-per-job mode for stricter production isolation

## Decision Summary

In the grand scheme, your idea is good and worth pursuing.

The best version of it is:

- **Not** one brand new environment per HTTP request.
- **Yes** one isolated executor per access job or session.
- **Yes** a strong separation between AI agents and privileged browser execution.
- **Yes** Docker first for adoption, Kubernetes later for stronger isolation and scale.

That gives Plaidify a practical path from developer-friendly local usage to a safer multi-tenant agent platform.