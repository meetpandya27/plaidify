import { useCallback, useEffect, useMemo, useReducer, useState } from "react";

import {
  flowReducer,
  initialFlowState,
  type FlowState,
  type Institution,
} from "./state";

/**
 * Minimal hosted-link React shell that matches the DOM contract the
 * Playwright E2E suite (tests/test_hosted_link_e2e.py) depends on:
 *
 *   - #step-select.active with #institution-search + .institution-item
 *   - #step-credentials.active with #provider-name, #consent-list,
 *     #link-username, #link-password, #connect-btn
 *   - #step-success.active with #success-message + #access-token-display
 *
 * The real search, MFA, and API wiring land in #68 when FastAPI starts
 * serving this bundle behind HOSTED_LINK_FRONTEND=react. Until then
 * this component is intentionally self-contained and safe to build.
 */

export interface AppProps {
  /** Optional seed institutions; defaults to none (caller fetches later). */
  readonly institutions?: readonly Institution[];
  /** Seed state — useful for tests. */
  readonly initialState?: FlowState;
}

export function App(props: AppProps = {}) {
  const [state, dispatch] = useReducer(flowReducer, props.initialState ?? initialFlowState);
  const [query, setQuery] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const institutions = useMemo(() => props.institutions ?? [], [props.institutions]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) {
      return institutions;
    }
    return institutions.filter((item) => item.name.toLowerCase().includes(q));
  }, [institutions, query]);

  useEffect(() => {
    if (state.step !== "credentials") {
      setUsername("");
      setPassword("");
    }
  }, [state.step]);

  const onSelect = useCallback((institution: Institution) => {
    dispatch({ type: "SELECT_INSTITUTION", institution });
  }, []);

  const onSubmitCredentials = useCallback(() => {
    dispatch({ type: "SUBMIT_CREDENTIALS" });
  }, []);

  // username/password are tracked for the form; actual transmission
  // lands in #68 when the API client is wired in.
  void username;
  void password;

  return (
    <main role="main" aria-label="Plaidify Link">
      <section
        id="step-select"
        className={state.step === "select" ? "link-step active" : "link-step"}
        role="region"
        aria-label="Select your provider"
      >
        <label className="sr-only" htmlFor="institution-search">
          Search providers
        </label>
        <input
          id="institution-search"
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search providers"
          autoComplete="off"
        />
        <ul id="institution-list" aria-label="Matching providers">
          {filtered.map((institution) => (
            <li
              key={institution.site}
              className="institution-item"
              role="button"
              tabIndex={0}
              onClick={() => onSelect(institution)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(institution);
                }
              }}
            >
              {institution.name}
            </li>
          ))}
        </ul>
      </section>

      <section
        id="step-credentials"
        className={state.step === "credentials" ? "link-step active" : "link-step"}
        role="region"
        aria-label="Enter your credentials"
      >
        <h2 id="provider-name">{state.institution?.name ?? ""}</h2>
        <ul id="consent-list">
          <li>Plaidify opens a secure session with your provider.</li>
          <li>Your sign-in details are encrypted before submission.</li>
          <li>Only the selected provider receives your encrypted credentials.</li>
        </ul>
        <label htmlFor="link-username">Username</label>
        <input
          id="link-username"
          type="text"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <label htmlFor="link-password">Password</label>
        <input
          id="link-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button id="connect-btn" type="button" onClick={onSubmitCredentials}>
          Connect
        </button>
      </section>

      <section
        id="step-success"
        className={state.step === "success" ? "link-step active" : "link-step"}
        role="region"
        aria-label="Connection successful"
      >
        <p id="success-message">{state.success?.summary ?? "Connected."}</p>
        <code id="access-token-display">{state.success?.accessToken ?? ""}</code>
      </section>
    </main>
  );
}
