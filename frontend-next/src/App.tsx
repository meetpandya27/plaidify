import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";

import {
  encryptCredentials as defaultEncryptCredentials,
  LinkApi,
  pollLinkSession as defaultPollLinkSession,
  type ConnectResponse,
  type LinkSessionStatus,
  type Organization,
  type PollOptions,
} from "./api";
import { detectNativeBridges, readHostedLinkConfig } from "./config";
import { EventDelivery, postBridgeEvent } from "./events";
import {
  flowReducer,
  initialFlowState,
  type FlowState,
  type Institution,
} from "./state";

const CONSENT_BULLETS: readonly string[] = [
  "Open a secure browser session with the provider you choose.",
  "Encrypt your sign-in details before they leave this window.",
  "Return a secure completion back to your app when verification finishes.",
];

const SUCCESS_MESSAGE =
  "Your secure connection is complete. Return to your app to finish setup.";

type EncryptCredentialsFn = typeof defaultEncryptCredentials;
type PollLinkSessionFn = (options: PollOptions) => Promise<LinkSessionStatus>;

interface ApiFactoryOptions {
  readonly serverUrl: string;
  readonly linkToken: string;
}

export interface AppProps {
  /** Seed state — used by unit tests. */
  readonly initialState?: FlowState;
  /** Overrides the default LinkApi (tests / storybook). */
  readonly apiFactory?: (options: ApiFactoryOptions) => LinkApi;
  /** Overrides the RSA-OAEP encryption helper (tests). */
  readonly encryptCredentials?: EncryptCredentialsFn;
  /** Overrides the status poller (tests). */
  readonly pollLinkSession?: PollLinkSessionFn;
  /** Overrides EventDelivery construction (tests). */
  readonly buildEventDelivery?: (
    options: { linkToken: string; serverUrl: string },
  ) => EventDelivery | null;
  /** Default institution list (used before the first search completes). */
  readonly seedInstitutions?: readonly Organization[];
}

export function App(props: AppProps = {}) {
  const [state, dispatch] = useReducer(flowReducer, props.initialState ?? initialFlowState);
  const [query, setQuery] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [organizations, setOrganizations] = useState<readonly Organization[]>(
    props.seedInstitutions ?? [],
  );
  const [searchError, setSearchError] = useState<string | null>(null);

  const configRef = useRef(
    readHostedLinkConfig(
      typeof window !== "undefined"
        ? window.location
        : ({ search: "", origin: "" } as Location),
      {
        referrer: typeof document !== "undefined" ? document.referrer : "",
        inIframe:
          typeof window !== "undefined" ? window.parent !== window : false,
      },
    ),
  );

  const apiRef = useRef<LinkApi | null>(null);
  const deliveryRef = useRef<EventDelivery | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const siteRef = useRef<string | null>(null);

  const encryptFn = props.encryptCredentials ?? defaultEncryptCredentials;
  const pollFn = props.pollLinkSession ?? defaultPollLinkSession;

  // Build the API client + event delivery once per linkToken.
  if (apiRef.current === null && configRef.current.linkToken) {
    const factory = props.apiFactory ?? ((opts) => new LinkApi(opts));
    apiRef.current = factory({
      serverUrl: configRef.current.serverUrl,
      linkToken: configRef.current.linkToken,
    });
    const buildDelivery =
      props.buildEventDelivery ??
      ((opts) => new EventDelivery(opts));
    deliveryRef.current = buildDelivery({
      linkToken: configRef.current.linkToken,
      serverUrl: configRef.current.serverUrl,
    });
  }

  const emit = useCallback(
    (event: string, payload: Record<string, unknown> = {}) => {
      const cfg = configRef.current;
      const bridges =
        typeof globalThis !== "undefined"
          ? detectNativeBridges(globalThis)
          : { reactNative: null, webkit: null };
      postBridgeEvent(event, payload, {
        parentOrigin: cfg.parentOrigin,
        inIframe: cfg.inIframe,
        targetWindow:
          typeof window !== "undefined" && cfg.inIframe ? window.parent : null,
        reactNativeBridge: bridges.reactNative,
        webkitBridge: bridges.webkit,
      });
      deliveryRef.current?.enqueue(event, payload);
    },
    [],
  );

  // Validate the session token and announce OPEN on mount.
  useEffect(() => {
    const api = apiRef.current;
    if (!api) {
      return;
    }
    let cancelled = false;
    emit("OPEN", {});
    api
      .getStatus()
      .then((status) => {
        if (cancelled) return;
        if (status.status === "expired" || status.status === "completed") {
          dispatch({
            type: "FAIL",
            payload: {
              message: `This link has ${status.status}. Request a fresh link to continue.`,
            },
          });
          return;
        }
        if (status.site) {
          siteRef.current = status.site;
        }
      })
      .catch((err: Error) => {
        if (cancelled) return;
        dispatch({
          type: "FAIL",
          payload: { message: err.message || "This link is invalid or has expired." },
        });
      });
    return () => {
      cancelled = true;
    };
  }, [emit]);

  // Debounced organization search tied to the query input.
  useEffect(() => {
    const api = apiRef.current;
    if (!api) {
      return;
    }
    const handle = globalThis.setTimeout(async () => {
      try {
        const payload = await api.searchOrganizations({ query, limit: 40 });
        setOrganizations(payload.results);
        setSearchError(null);
      } catch (err) {
        setSearchError((err as Error).message || "Could not load providers.");
      }
    }, 180);
    return () => globalThis.clearTimeout(handle);
  }, [query]);

  // Emit EXIT on unmount so the server can reconcile abandoned sessions.
  useEffect(() => {
    return () => {
      emit("EXIT", { reason: "unmount" });
      deliveryRef.current?.dispose();
    };
  }, [emit]);

  const onSelectInstitution = useCallback(
    (organization: Organization) => {
      const institution: Institution = {
        site: organization.site,
        name: organization.name,
        category: organization.category_label,
        country: organization.country_code,
        logo_url: organization.logo_url,
        primary_color: organization.primary_color,
        secondary_color: organization.secondary_color,
        accent_color: organization.accent_color,
        hint_copy: organization.hint_copy,
        auth_style: organization.auth_style,
      };
      siteRef.current = organization.site;
      dispatch({ type: "SELECT_INSTITUTION", institution });
      emit("INSTITUTION_SELECTED", {
        organization_id: organization.organization_id,
        organization_name: organization.name,
        site: organization.site,
      });
    },
    [emit],
  );

  const runConnect = useCallback(
    async (credsUsername: string, credsPassword: string) => {
      const api = apiRef.current;
      if (!api) {
        dispatch({
          type: "FAIL",
          payload: { message: "Session is not initialized." },
        });
        return;
      }
      const site = siteRef.current;
      if (!site) {
        dispatch({
          type: "FAIL",
          payload: { message: "Choose a provider before continuing." },
        });
        return;
      }
      dispatch({ type: "SUBMIT_CREDENTIALS" });
      try {
        const keyPayload = await api.getEncryptionPublicKey();
        if (!keyPayload.public_key) {
          throw new Error("Unable to establish an encrypted session.");
        }
        const encrypted = await encryptFn(
          keyPayload.public_key,
          credsUsername,
          credsPassword,
        );
        const response = await api.connect({ site, encrypted });
        await handleConnectResponse(api, response);
      } catch (err) {
        const message = (err as Error).message || "Connection failed.";
        dispatch({ type: "FAIL", payload: { message } });
        emit("ERROR", { error: message, site });
      }
    },
    [emit, encryptFn],
  );

  const handleConnectResponse = useCallback(
    async (api: LinkApi, response: ConnectResponse) => {
      if (response.session_id) {
        sessionIdRef.current = response.session_id;
      }
      if (response.status === "connected") {
        await finishSuccess(api, response);
        return;
      }
      if (response.status === "mfa_required") {
        const message =
          (response.metadata as { message?: string } | null)?.message ??
          "Enter the verification code from your provider to continue.";
        dispatch({ type: "MFA_REQUIRED", prompt: message });
        emit("MFA_REQUIRED", {
          mfa_type: response.mfa_type ?? "otp",
          session_id: response.session_id ?? null,
        });
        return;
      }
      if (response.status === "pending") {
        const terminal = await pollFn({ api });
        if (terminal.status === "completed") {
          await finishSuccess(api, terminal);
          return;
        }
        if (terminal.status === "mfa_required") {
          dispatch({
            type: "MFA_REQUIRED",
            prompt:
              terminal.message ||
              "Enter the verification code from your provider to continue.",
          });
          emit("MFA_REQUIRED", {
            mfa_type: terminal.mfa_type ?? "otp",
            session_id: terminal.session_id ?? null,
          });
          return;
        }
        const message =
          terminal.error_message ||
          terminal.message ||
          (terminal.status === "timeout"
            ? "The connection timed out before the provider completed the flow."
            : "The connection could not be completed.");
        dispatch({ type: "FAIL", payload: { message } });
        emit("ERROR", { error: message, site: siteRef.current });
        return;
      }
      const message =
        response.error || response.detail || `Unexpected status: ${response.status}`;
      dispatch({ type: "FAIL", payload: { message } });
      emit("ERROR", { error: message, site: siteRef.current });
    },
    [emit, pollFn],
  );

  const finishSuccess = useCallback(
    async (api: LinkApi, payload: ConnectResponse | LinkSessionStatus) => {
      // /connect may not include public_token on the first response; the
      // backend stores it on the session and surfaces it through the
      // status endpoint, matching the legacy page's resolveHostedSuccess.
      let resolved: ConnectResponse | LinkSessionStatus = payload;
      if (!resolved.public_token) {
        try {
          const status = await api.getStatus();
          if (status.status === "completed" && status.public_token) {
            resolved = status;
          }
        } catch {
          // Fall back to whatever we already have.
        }
      }
      const publicToken = resolved.public_token ?? "";
      dispatch({
        type: "SUCCEED",
        payload: { accessToken: publicToken, summary: SUCCESS_MESSAGE },
      });
      emit("CONNECTED", {
        job_id: resolved.job_id ?? null,
        public_token: publicToken,
        site: siteRef.current,
      });
    },
    [emit],
  );

  const onSubmitCredentials = useCallback(() => {
    void runConnect(username, password);
  }, [password, runConnect, username]);

  const onSubmitMfa = useCallback(async () => {
    const api = apiRef.current;
    const sessionId = sessionIdRef.current;
    if (!api || !sessionId || !mfaCode.trim()) {
      return;
    }
    dispatch({ type: "SUBMIT_MFA" });
    emit("MFA_SUBMITTED", { session_id: sessionId });
    try {
      const response = await api.submitMfa({
        sessionId,
        code: mfaCode.trim(),
      });
      await handleConnectResponse(api, response);
    } catch (err) {
      const message = (err as Error).message || "Verification failed.";
      dispatch({ type: "FAIL", payload: { message } });
      emit("ERROR", { error: message, site: siteRef.current });
    }
  }, [emit, handleConnectResponse, mfaCode]);

  const consent = useMemo(() => CONSENT_BULLETS, []);

  return (
    <main role="main" aria-label="Plaidify Link" className="plaidify-link">
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
        {searchError ? (
          <p className="search-error" role="alert">
            {searchError}
          </p>
        ) : null}
        <ul id="institution-list" aria-label="Matching providers">
          {organizations.map((organization) => (
            <li
              key={organization.organization_id || organization.site}
              className="institution-item"
              role="button"
              tabIndex={0}
              data-organization-id={organization.organization_id}
              style={
                organization.primary_color
                  ? ({
                      "--organization-primary": organization.primary_color,
                      "--organization-secondary":
                        organization.secondary_color ?? "transparent",
                    } as React.CSSProperties)
                  : undefined
              }
              onClick={() => onSelectInstitution(organization)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelectInstitution(organization);
                }
              }}
            >
              {organization.logo_url ? (
                <img
                  className="institution-item__logo"
                  src={organization.logo_url}
                  alt=""
                  width={32}
                  height={32}
                  aria-hidden="true"
                />
              ) : (
                <span
                  className="institution-item__monogram"
                  aria-hidden="true"
                  style={
                    organization.primary_color
                      ? {
                          background: organization.primary_color,
                          color: organization.secondary_color ?? "#fff",
                        }
                      : undefined
                  }
                >
                  {organization.logo_monogram ?? organization.name.slice(0, 1)}
                </span>
              )}
              <span className="institution-item__name">{organization.name}</span>
              {organization.category_label ? (
                <span className="institution-item__category">
                  {organization.category_label}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      </section>

      <section
        id="step-credentials"
        className={state.step === "credentials" ? "link-step active" : "link-step"}
        role="region"
        aria-label="Enter your credentials"
        style={
          state.institution?.primary_color
            ? ({
                "--organization-primary": state.institution.primary_color,
                "--organization-secondary":
                  state.institution.secondary_color ?? "transparent",
                "--organization-accent":
                  state.institution.accent_color ?? state.institution.primary_color,
              } as React.CSSProperties)
            : undefined
        }
      >
        <header className="credentials-header">
          {state.institution?.logo_url ? (
            <img
              className="credentials-header__logo"
              src={state.institution.logo_url}
              alt=""
              width={48}
              height={48}
              aria-hidden="true"
            />
          ) : null}
          <h2 id="provider-name">{state.institution?.name ?? ""}</h2>
        </header>
        {state.institution?.hint_copy ? (
          <p id="provider-hint" className="credentials-hint">
            {state.institution.hint_copy}
          </p>
        ) : null}
        <ul id="consent-list">
          {consent.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
        <label htmlFor="link-username">
          {state.institution?.auth_style === "email_password"
            ? "Email"
            : state.institution?.auth_style === "member_number"
              ? "Member number"
              : "Username"}
        </label>
        <input
          id="link-username"
          type={state.institution?.auth_style === "email_password" ? "email" : "text"}
          autoComplete={
            state.institution?.auth_style === "email_password" ? "email" : "username"
          }
          inputMode={
            state.institution?.auth_style === "member_number" ? "numeric" : undefined
          }
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
          Continue
        </button>
      </section>

      <section
        id="step-connecting"
        className={state.step === "connecting" ? "link-step active" : "link-step"}
        role="region"
        aria-label="Connecting"
      >
        <p>Creating your secure session&hellip;</p>
      </section>

      <section
        id="step-mfa"
        className={state.step === "mfa" ? "link-step active" : "link-step"}
        role="region"
        aria-label="Finish verification"
      >
        <p id="mfa-message">{state.mfaPrompt ?? ""}</p>
        <label htmlFor="mfa-code">Verification code</label>
        <input
          id="mfa-code"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          value={mfaCode}
          onChange={(e) => setMfaCode(e.target.value)}
        />
        <button id="mfa-submit-btn" type="button" onClick={() => void onSubmitMfa()}>
          Verify and continue
        </button>
      </section>

      <section
        id="step-success"
        className={state.step === "success" ? "link-step active" : "link-step"}
        role="region"
        aria-label="Connection successful"
      >
        <p id="success-message">{state.success?.summary ?? SUCCESS_MESSAGE}</p>
        <div id="access-token-display">
          {state.success?.accessToken ? (
            <div className="reference-row">
              <span className="reference-label">PUBLIC TOKEN</span>
              <span className="reference-value">{state.success.accessToken}</span>
            </div>
          ) : null}
        </div>
      </section>

      <section
        id="step-error"
        className={state.step === "error" ? "link-step active" : "link-step"}
        role="region"
        aria-label="Connection failed"
      >
        <p id="error-message">{state.error?.message ?? ""}</p>
        <button
          id="retry-btn"
          type="button"
          onClick={() => dispatch({ type: "BACK_TO_PICKER" })}
        >
          Try again
        </button>
      </section>
    </main>
  );
}
