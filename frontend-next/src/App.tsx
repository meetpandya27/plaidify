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
  type CredentialSchema,
  type LinkSessionStatus,
  type MfaSchema,
  type MfaSchemaEntry,
  type Organization,
  type PollOptions,
  type SchemaField,
} from "./api";
import { detectNativeBridges, readHostedLinkConfig } from "./config";
import { DynamicForm, validateSchemaValues } from "./DynamicForm";
import {
  classifyError,
  remediationFor,
  type LinkErrorCode,
  type RemediationAction,
} from "./errorTaxonomy";
import { EventDelivery, postBridgeEvent } from "./events";
import {
  DEFAULT_LOCALE,
  type Locale,
  type Messages,
  getMessages,
  resolveLocale,
} from "./i18n";
import {
  flowReducer,
  initialFlowState,
  type FlowState,
  type Institution,
} from "./state";

// NOTE: The canonical English copy for the E2E DOM contract now lives
// in `./i18n` (the `en-US` catalog). Keep the following strings stable
// when evolving that catalog: the third consent bullet must read
// "Return a secure completion back to your app when verification
// finishes.", the success message must contain "Return to your app",
// and the public-token label must be "PUBLIC TOKEN".

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
  /** Override the negotiated UI locale (tests / storybook). */
  readonly locale?: Locale;
}

export function App(props: AppProps = {}) {
  const [state, dispatch] = useReducer(flowReducer, props.initialState ?? initialFlowState);
  const [query, setQuery] = useState("");
  const [credentialValues, setCredentialValues] = useState<Readonly<Record<string, string>>>({});
  const [credentialErrors, setCredentialErrors] = useState<Readonly<Record<string, string>>>({});
  const [mfaValues, setMfaValues] = useState<Readonly<Record<string, string>>>({});
  const [mfaErrors, setMfaErrors] = useState<Readonly<Record<string, string>>>({});
  const [mfaType, setMfaType] = useState<string>("otp_input");
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
  const stepHeadingRef = useRef<HTMLElement | null>(null);
  const previousStepRef = useRef<string>(state.step);
  const [liveAnnouncement, setLiveAnnouncement] = useState("");

  const locale: Locale =
    props.locale ??
    (typeof window !== "undefined"
      ? resolveLocale({
          search: window.location.search,
          navigatorLanguages:
            typeof navigator !== "undefined"
              ? [...(navigator.languages ?? [navigator.language ?? "en-US"])]
              : undefined,
        })
      : DEFAULT_LOCALE);
  const messages: Messages = useMemo(() => getMessages(locale), [locale]);

  const encryptFn = props.encryptCredentials ?? defaultEncryptCredentials;
  const pollFn = props.pollLinkSession ?? defaultPollLinkSession;

  const credentialSchema: CredentialSchema = useMemo(
    () =>
      state.institution?.credential_schema ?? fallbackCredentialSchema(state.institution?.auth_style),
    [state.institution],
  );
  const mfaSchemaEntry: MfaSchemaEntry = useMemo(
    () => resolveMfaSchemaEntry(state.institution?.mfa_schema, mfaType),
    [state.institution, mfaType],
  );

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
      emit("EXIT", {
        reason: "unmount",
        error_code: state.error?.code ?? null,
      });
      deliveryRef.current?.dispose();
    };
  }, [emit, state.error?.code]);

  // Focus management + polite announcement on step transition (#56 a11y).
  useEffect(() => {
    if (previousStepRef.current === state.step) {
      return;
    }
    previousStepRef.current = state.step;
    const target = stepHeadingRef.current;
    if (target) {
      // Ensure programmatic focus works without showing a persistent tabindex.
      if (!target.hasAttribute("tabindex")) {
        target.setAttribute("tabindex", "-1");
      }
      try {
        target.focus({ preventScroll: false });
      } catch {
        target.focus();
      }
    }
    const announcements: Record<string, string> = {
      select: messages.live_select,
      credentials: messages.live_credentials,
      connecting: messages.live_connecting,
      mfa: messages.live_mfa,
      success: messages.live_success,
      error: state.error?.message
        ? `${messages.live_error} ${state.error.message}`
        : messages.live_error,
    };
    setLiveAnnouncement(announcements[state.step] ?? "");
  }, [state.step, state.error?.message, messages]);

  const failWith = useCallback(
    (err: unknown, options: { fallbackCode?: LinkErrorCode; site?: string | null } = {}) => {
      const resolvedCode: LinkErrorCode = (() => {
        const classified = classifyError(err);
        if (classified === "internal_error" && options.fallbackCode) {
          return options.fallbackCode;
        }
        return classified;
      })();
      const message =
        (err && typeof err === "object" && (err as { message?: unknown }).message) ||
        (typeof err === "string" ? err : "") ||
        "Something went wrong.";
      dispatch({
        type: "FAIL",
        payload: { message: String(message), code: resolvedCode },
      });
      emit("ERROR", {
        error: String(message),
        error_code: resolvedCode,
        site: options.site ?? siteRef.current,
      });
    },
    [emit],
  );

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
        failWith(new Error("Session is not initialized."), {
          fallbackCode: "internal_error",
        });
        return;
      }
      const site = siteRef.current;
      if (!site) {
        failWith(new Error("Choose a provider before continuing."), {
          fallbackCode: "internal_error",
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
        failWith(err instanceof Error ? err : new Error(message), {
          fallbackCode: "network_error",
          site,
        });
      }
    },
    [emit, encryptFn, failWith],
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
        setMfaType(response.mfa_type ?? "otp_input");
        setMfaValues({});
        setMfaErrors({});
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
          setMfaType(terminal.mfa_type ?? "otp_input");
          setMfaValues({});
          setMfaErrors({});
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
        const fallback: LinkErrorCode =
          terminal.status === "timeout" ? "mfa_timeout" : "internal_error";
        failWith(new Error(message), { fallbackCode: fallback });
        return;
      }
      const message =
        response.error || response.detail || `Unexpected status: ${response.status}`;
      failWith(new Error(message), { fallbackCode: "internal_error" });
    },
    [emit, failWith, pollFn],
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
        payload: { accessToken: publicToken, summary: messages.success_message },
      });
      emit("CONNECTED", {
        job_id: resolved.job_id ?? null,
        public_token: publicToken,
        site: siteRef.current,
      });
    },
    [emit, messages],
  );

  const onSubmitCredentials = useCallback(() => {
    const errors = validateSchemaValues(credentialSchema.fields, credentialValues);
    if (errors.length) {
      const map: Record<string, string> = {};
      for (const err of errors) map[err.field] = err.message;
      setCredentialErrors(map);
      return;
    }
    setCredentialErrors({});
    const usernameValue = credentialValues.username ?? "";
    const passwordValue = credentialValues.password ?? "";
    void runConnect(usernameValue.trim(), passwordValue);
  }, [credentialSchema, credentialValues, runConnect]);

  const onSubmitMfa = useCallback(async () => {
    const api = apiRef.current;
    const sessionId = sessionIdRef.current;
    if (!api || !sessionId) {
      return;
    }
    const errors = validateSchemaValues(mfaSchemaEntry.fields, mfaValues);
    if (errors.length) {
      const map: Record<string, string> = {};
      for (const err of errors) map[err.field] = err.message;
      setMfaErrors(map);
      return;
    }
    setMfaErrors({});
    const codeValue = (mfaValues.code ?? "").trim();
    // Push-type prompts omit fields — treat the submit click as confirmation.
    if (!codeValue && mfaSchemaEntry.fields.length > 0) {
      return;
    }
    dispatch({ type: "SUBMIT_MFA" });
    emit("MFA_SUBMITTED", { session_id: sessionId });
    try {
      const response = await api.submitMfa({
        sessionId,
        code: codeValue,
      });
      await handleConnectResponse(api, response);
    } catch (err) {
      const message = (err as Error).message || "Verification failed.";
      failWith(err instanceof Error ? err : new Error(message), {
        fallbackCode: "mfa_timeout",
      });
    }
  }, [emit, failWith, handleConnectResponse, mfaSchemaEntry, mfaValues]);

  const consent = useMemo(() => messages.consent_bullets, [messages]);

  return (
    <main role="main" aria-label="Plaidify Link" className="plaidify-link" lang={locale}>
      <div
        id="link-live-region"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="sr-only"
      >
        {liveAnnouncement}
      </div>
      <section
        id="step-select"
        className={state.step === "select" ? "link-step active" : "link-step"}
        role="region"
        aria-label={messages.step_select_heading}
      >
        <h2
          id="step-select-heading"
          className="sr-only"
          ref={(el) => {
            if (state.step === "select") stepHeadingRef.current = el;
          }}
          tabIndex={-1}
        >
          {messages.step_select_heading}
        </h2>
        <label className="sr-only" htmlFor="institution-search">
          {messages.search_label}
        </label>
        <input
          id="institution-search"
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={messages.search_placeholder}
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
            >
              <button
                type="button"
                className="institution-item__button"
                data-organization-id={organization.organization_id}
                aria-label={
                  organization.category_label
                    ? `${organization.name}, ${organization.category_label}`
                    : organization.name
                }
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
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section
        id="step-credentials"
        className={state.step === "credentials" ? "link-step active" : "link-step"}
        role="region"
        aria-label={messages.step_credentials_heading}
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
          <h2
            id="provider-name"
            ref={(el) => {
              if (state.step === "credentials") stepHeadingRef.current = el;
            }}
            tabIndex={-1}
          >
            {state.institution?.name ?? ""}
          </h2>
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
        <DynamicForm
          fields={credentialSchema.fields}
          values={credentialValues}
          errors={credentialErrors}
          onChange={(id, value) => {
            setCredentialValues((prev) => ({ ...prev, [id]: value }));
            if (credentialErrors[id]) {
              setCredentialErrors((prev) => {
                const next = { ...prev };
                delete next[id];
                return next;
              });
            }
          }}
          onBlur={(id) => {
            const field = credentialSchema.fields.find((f) => f.id === id);
            if (!field) return;
            const errors = validateSchemaValues([field], credentialValues);
            if (errors.length) {
              setCredentialErrors((prev) => ({ ...prev, [id]: errors[0].message }));
            }
          }}
        />
        <button id="connect-btn" type="button" onClick={onSubmitCredentials}>
          {credentialSchema.submit_label ?? messages.continue_cta}
        </button>
      </section>

      <section
        id="step-connecting"
        className={state.step === "connecting" ? "link-step active" : "link-step"}
        role="region"
        aria-label={messages.step_connecting_heading}
      >
        <p
          role="status"
          ref={(el) => {
            if (state.step === "connecting") stepHeadingRef.current = el;
          }}
          tabIndex={-1}
        >
          {messages.step_connecting_body}
        </p>
      </section>

      <section
        id="step-mfa"
        className={state.step === "mfa" ? "link-step active" : "link-step"}
        role="region"
        aria-label={messages.step_mfa_heading}
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
        {mfaSchemaEntry.title ? (
          <h2
            id="mfa-title"
            ref={(el) => {
              if (state.step === "mfa") stepHeadingRef.current = el;
            }}
            tabIndex={-1}
          >
            {mfaSchemaEntry.title}
          </h2>
        ) : null}
        <p
          id="mfa-message"
          ref={(el) => {
            if (state.step === "mfa" && !mfaSchemaEntry.title)
              stepHeadingRef.current = el;
          }}
          tabIndex={-1}
        >
          {state.mfaPrompt ?? ""}
        </p>
        {mfaSchemaEntry.help_text ? (
          <p id="mfa-help" className="credentials-hint">
            {mfaSchemaEntry.help_text}
          </p>
        ) : null}
        <DynamicForm
          idPrefix="mfa"
          fields={mfaSchemaEntry.fields}
          values={mfaValues}
          errors={mfaErrors}
          onChange={(id, value) => {
            setMfaValues((prev) => ({ ...prev, [id]: value }));
            if (mfaErrors[id]) {
              setMfaErrors((prev) => {
                const next = { ...prev };
                delete next[id];
                return next;
              });
            }
          }}
          onBlur={(id) => {
            const field = mfaSchemaEntry.fields.find((f) => f.id === id);
            if (!field) return;
            const errors = validateSchemaValues([field], mfaValues);
            if (errors.length) {
              setMfaErrors((prev) => ({ ...prev, [id]: errors[0].message }));
            }
          }}
        />
        <button id="mfa-submit-btn" type="button" onClick={() => void onSubmitMfa()}>
          {mfaSchemaEntry.submit_label ?? messages.verify_cta}
        </button>
      </section>

      <section
        id="step-success"
        className={state.step === "success" ? "link-step active" : "link-step"}
        role="region"
        aria-label={messages.step_success_heading}
      >
        <p
          id="success-message"
          ref={(el) => {
            if (state.step === "success") stepHeadingRef.current = el;
          }}
          tabIndex={-1}
        >
          {state.success?.summary ?? messages.success_message}
        </p>
        <div id="access-token-display">
          {state.success?.accessToken ? (
            <div className="reference-row">
              <span className="reference-label">{messages.public_token_label}</span>
              <span className="reference-value">{state.success.accessToken}</span>
            </div>
          ) : null}
        </div>
      </section>

      <section
        id="step-error"
        className={state.step === "error" ? "link-step active" : "link-step"}
        role="region"
        aria-label={messages.step_error_heading}
        aria-live="assertive"
        data-error-code={state.error?.code ?? "internal_error"}
      >
        {(() => {
          const remediation = remediationFor(state.error?.code);
          const handleAction = (action: RemediationAction) => {
            if (action === "retry") {
              dispatch({ type: "BACK_TO_PICKER" });
            } else if (action === "back_to_picker") {
              dispatch({ type: "BACK_TO_PICKER" });
            } else if (action === "contact_support") {
              emit("SUPPORT_REQUESTED", {
                error_code: state.error?.code ?? "internal_error",
              });
            } else if (action === "exit") {
              emit("EXIT", {
                reason: "user_exit",
                error_code: state.error?.code ?? null,
              });
            }
          };
          return (
            <>
              <h2
                id="error-title"
                ref={(el) => {
                  if (state.step === "error") stepHeadingRef.current = el;
                }}
                tabIndex={-1}
              >
                {remediation.title}
              </h2>
              <p id="error-description">{remediation.description}</p>
              <p id="error-message" className="sr-only">
                {state.error?.message ?? ""}
              </p>
              <div className="error-actions">
                <button
                  id="retry-btn"
                  type="button"
                  onClick={() => handleAction(remediation.primary_action)}
                >
                  {remediation.primary_cta}
                </button>
                {remediation.secondary_cta && remediation.secondary_action ? (
                  <button
                    id="error-secondary-btn"
                    type="button"
                    className="secondary"
                    onClick={() => handleAction(remediation.secondary_action!)}
                  >
                    {remediation.secondary_cta}
                  </button>
                ) : null}
              </div>
            </>
          );
        })()}
      </section>
    </main>
  );
}

// ── Schema fallbacks ─────────────────────────────────────────────────────────
// Mirror `organization_catalog._default_credential_schema` and
// `_default_mfa_schema` so the UI still renders something sensible if the
// backend omits them (e.g. older catalog payload or unit tests).

const DEFAULT_CRED_FIELDS: Record<string, readonly SchemaField[]> = {
  username_password: [
    {
      id: "username",
      label: "Username",
      type: "text",
      autocomplete: "username",
      required: true,
      min_length: 3,
      max_length: 128,
    },
    {
      id: "password",
      label: "Password",
      type: "password",
      autocomplete: "current-password",
      required: true,
      secret: true,
      reveal: true,
      min_length: 6,
      max_length: 128,
    },
  ],
  email_password: [
    {
      id: "username",
      label: "Email",
      type: "email",
      autocomplete: "email",
      required: true,
      min_length: 5,
      max_length: 254,
    },
    {
      id: "password",
      label: "Password",
      type: "password",
      autocomplete: "current-password",
      required: true,
      secret: true,
      reveal: true,
      min_length: 6,
      max_length: 128,
    },
  ],
  member_number: [
    {
      id: "username",
      label: "Member number",
      type: "text",
      autocomplete: "username",
      inputmode: "numeric",
      required: true,
      min_length: 4,
      max_length: 32,
    },
    {
      id: "password",
      label: "Password",
      type: "password",
      autocomplete: "current-password",
      required: true,
      secret: true,
      reveal: true,
      min_length: 6,
      max_length: 128,
    },
  ],
};

function fallbackCredentialSchema(authStyle?: string): CredentialSchema {
  const fields =
    DEFAULT_CRED_FIELDS[authStyle ?? "username_password"] ??
    DEFAULT_CRED_FIELDS.username_password;
  return { submit_label: "Connect securely", fields };
}

const DEFAULT_MFA_ENTRY: MfaSchemaEntry = {
  title: "Enter your verification code",
  help_text: "Check your phone, email, or authenticator app for the code.",
  submit_label: "Verify and continue",
  fields: [
    {
      id: "code",
      label: "Verification code",
      type: "text",
      inputmode: "numeric",
      autocomplete: "one-time-code",
      pattern: "^\\d{4,8}$",
      min_length: 4,
      max_length: 8,
      required: true,
    },
  ],
};

function resolveMfaSchemaEntry(schema: MfaSchema | undefined, type: string): MfaSchemaEntry {
  return (schema?.[type] ?? schema?.otp_input ?? DEFAULT_MFA_ENTRY) as MfaSchemaEntry;
}
