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
  mfaAttemptsRemaining,
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
import { resolveBranding, type Branding } from "./branding";
import { detectNativeBridges, readHostedLinkConfig, type HostedLinkConfig } from "./config";
import { DynamicForm, validateSchemaValues } from "./DynamicForm";
import {
  classifyError,
  remediationFor,
  type LinkErrorCode,
  type RemediationAction,
} from "./errorTaxonomy";
import { EventDelivery, ParentChannel, postBridgeEvent } from "./events";
import {
  DEFAULT_LOCALE,
  type Locale,
  type Messages,
  getMessages,
  resolveLocale,
} from "./i18n";
import { SkeletonRowList } from "./Skeleton";
import { createTelemetry, type Telemetry } from "./telemetry";
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
// finishes." and the success message must contain "Return to your app".
// The public token is never rendered: it reaches the embedding app
// through the CONNECTED event only.

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
  /** Overrides the embedder branding read from the URL (tests / storybook). */
  readonly branding?: Branding;
  /** Overrides the channel to the embedding window (tests). */
  readonly buildParentChannel?: (
    config: HostedLinkConfig,
  ) => Pick<ParentChannel, "post" | "resolve"> | null;
}

type ParentChannelLike = Pick<ParentChannel, "post" | "resolve">;

function defaultParentChannel(config: HostedLinkConfig): ParentChannelLike | null {
  if (!config.inIframe || typeof window === "undefined") {
    return null;
  }
  return new ParentChannel({
    targetWindow: window.parent,
    ownOrigin: config.ownOrigin,
    candidateOrigin: config.parentOrigin,
  });
}

/** Keep what the user typed except secrets (a retry re-asks for those). */
function withoutSecrets(
  values: Readonly<Record<string, string>>,
  fields: readonly SchemaField[],
): Record<string, string> {
  const secret = new Set(
    fields.filter((field) => field.type === "password" || field.secret).map((field) => field.id),
  );
  const kept: Record<string, string> = {};
  for (const [id, value] of Object.entries(values)) {
    if (!secret.has(id)) kept[id] = value;
  }
  return kept;
}

/** Session states in which the page may still pick the provider for the user. */
const PRE_CONNECT_STATUSES = new Set(["awaiting_institution", "awaiting_credentials"]);

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
        allowServerOverride: import.meta.env.DEV,
      },
    ),
  );

  const apiRef = useRef<LinkApi | null>(null);
  const deliveryRef = useRef<EventDelivery | null>(null);
  const parentRef = useRef<ParentChannelLike | null | undefined>(undefined);
  const sessionIdRef = useRef<string | null>(null);
  // attempts_remaining of the MFA challenge on screen (null before the site
  // has rejected a code), so a re-opened challenge reads as a new prompt.
  const mfaRemainingRef = useRef<number | null>(null);
  const siteRef = useRef<string | null>(null);
  // Latest state for callbacks that outlive the render that made them.
  const stateRef = useRef<FlowState>(state);
  stateRef.current = state;
  const openSentRef = useRef(false);
  const exitSentRef = useRef(false);
  const completedRef = useRef(false);
  const submittingRef = useRef(false);
  const stepHeadingRef = useRef<HTMLElement | null>(null);
  const previousStepRef = useRef<string>(state.step);
  const [liveAnnouncement, setLiveAnnouncement] = useState("");
  const telemetryRef = useRef<Telemetry | null>(null);

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
  const branding: Branding = useMemo(
    () =>
      props.branding ??
      resolveBranding(typeof window !== "undefined" ? window.location.search : ""),
    [props.branding],
  );

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
  if (parentRef.current === undefined) {
    parentRef.current = (props.buildParentChannel ?? defaultParentChannel)(configRef.current);
  }

  const emit = useCallback(
    (event: string, payload: Record<string, unknown> = {}) => {
      const bridges =
        typeof globalThis !== "undefined"
          ? detectNativeBridges(globalThis)
          : { reactNative: null, webkit: null, android: null };
      postBridgeEvent(event, payload, {
        parent: parentRef.current ?? null,
        reactNativeBridge: bridges.reactNative,
        webkitBridge: bridges.webkit,
        androidBridge: bridges.android,
      });
      deliveryRef.current?.enqueue(event, payload);
    },
    [],
  );

  // Initialize structured UX telemetry (#61) once the emit pipeline
  // exists. Telemetry rides on the same bus under `TELEMETRY` so
  // downstream analytics can subscribe via SSE.
  if (telemetryRef.current === null) {
    telemetryRef.current = createTelemetry({ emit });
  }

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

  // Page teardown (tab closed, navigated away, webview destroyed). This is
  // the only implicit EXIT: re-renders and error changes never send one,
  // and nothing here drops queued events — they go out by beacon instead.
  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const onPageHide = () => {
      const delivery = deliveryRef.current;
      if (!delivery) {
        return;
      }
      if (!exitSentRef.current && !completedRef.current) {
        exitSentRef.current = true;
        const errorCode = stateRef.current.error?.code ?? null;
        telemetryRef.current?.exitReason("page_closed", errorCode ?? undefined);
        // Server only: whoever tore the page down already knows.
        delivery.enqueue("EXIT", { reason: "page_closed", error_code: errorCode });
      }
      delivery.flushOnTeardown();
    };
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
  }, []);

  const exitLink = useCallback(
    (reason: string) => {
      if (exitSentRef.current) {
        return;
      }
      exitSentRef.current = true;
      const errorCode = stateRef.current.error?.code ?? null;
      telemetryRef.current?.exitReason(reason, errorCode ?? undefined);
      emit("EXIT", { reason, error_code: errorCode });
    },
    [emit],
  );

  // Focus management + polite announcement on step transition (#56 a11y).
  useEffect(() => {
    if (previousStepRef.current === state.step) {
      return;
    }
    const fromStep = previousStepRef.current;
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
    // Telemetry (#61): step_complete for the prior step, step_view for the new.
    if (telemetryRef.current) {
      if (fromStep && fromStep !== state.step) {
        telemetryRef.current.stepComplete(fromStep);
      }
      telemetryRef.current.stepView(state.step);
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

  const clearCredentials = useCallback(() => {
    setCredentialValues({});
    setCredentialErrors({});
  }, []);

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
        // The provider's own form: field rules for credentials and one
        // entry per MFA type (security answers, push approval, ...).
        credential_schema: organization.credential_schema,
        mfa_schema: organization.mfa_schema,
      };
      siteRef.current = organization.site;
      // What was typed for another provider must never be sent to this one.
      clearCredentials();
      dispatch({ type: "SELECT_INSTITUTION", institution });
      emit("INSTITUTION_SELECTED", {
        organization_id: organization.organization_id,
        organization_name: organization.name,
        site: organization.site,
      });
      telemetryRef.current?.institutionSelected(organization.organization_id);
    },
    [clearCredentials, emit],
  );

  // Validate the session token, announce OPEN, and learn which origins may
  // embed the page. A session created for one site skips the picker.
  useEffect(() => {
    const api = apiRef.current;
    if (!api) {
      parentRef.current?.resolve([]);
      dispatch({
        type: "FAIL",
        payload: {
          message:
            configRef.current.tokenProblem === "duplicate"
              ? "This link is invalid. Request a fresh link to continue."
              : "This link is invalid or has expired.",
        },
      });
      return;
    }
    let cancelled = false;
    if (!openSentRef.current) {
      openSentRef.current = true;
      emit("OPEN", {});
    }
    api
      .getStatus()
      .then(async (status) => {
        if (cancelled) return;
        parentRef.current?.resolve(status.allowed_origins);
        if (status.status === "expired" || status.status === "completed") {
          dispatch({
            type: "FAIL",
            payload: {
              message: `This link has ${status.status}. Request a fresh link to continue.`,
            },
          });
          return;
        }
        if (!status.site) {
          return;
        }
        siteRef.current = status.site;
        if (!PRE_CONNECT_STATUSES.has(status.status)) {
          return;
        }
        try {
          const found = await api.searchOrganizations({ site: status.site, limit: 1 });
          const organization = found.results[0];
          // Only while the user has not picked something themselves.
          if (!cancelled && organization && stateRef.current.step === "select") {
            onSelectInstitution(organization);
          }
        } catch {
          // The picker still works; the user chooses the provider.
        }
      })
      .catch((err: Error) => {
        if (cancelled) return;
        parentRef.current?.resolve([]);
        dispatch({
          type: "FAIL",
          payload: { message: err.message || "This link is invalid or has expired." },
        });
      });
    return () => {
      cancelled = true;
    };
  }, [emit, onSelectInstitution]);

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
      if (submittingRef.current) {
        // Enter pressed twice: one attempt at a time.
        return;
      }
      submittingRef.current = true;
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
      } finally {
        submittingRef.current = false;
      }
    },
    [emit, encryptFn, failWith],
  );

  // The prompt for an MFA challenge. When the site rejected the last code,
  // say so (with the attempts left) instead of repeating the first prompt.
  const mfaPrompt = useCallback(
    (status: ConnectResponse | LinkSessionStatus, fallback: string): string => {
      const remaining = mfaAttemptsRemaining(status);
      mfaRemainingRef.current = remaining ?? null;
      if (status.metadata?.mfa_error !== "invalid_code") {
        return fallback;
      }
      return remaining === undefined
        ? messages.mfa_invalid_code
        : `${messages.mfa_invalid_code} ${messages.mfa_attempts_left.replace("{count}", String(remaining))}`;
    },
    [messages],
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
        const message = mfaPrompt(
          response,
          (response.metadata as { message?: string } | null)?.message ??
            "Enter the verification code from your provider to continue.",
        );
        setMfaType(response.mfa_type ?? "otp_input");
        setMfaValues({});
        setMfaErrors({});
        dispatch({ type: "MFA_REQUIRED", prompt: message });
        emit("MFA_REQUIRED", {
          mfa_type: response.mfa_type ?? "otp",
          session_id: response.session_id ?? null,
        });
        telemetryRef.current?.mfaShown(response.mfa_type ?? "otp");
        return;
      }
      // /mfa/submit answers "mfa_submitted" while the job carries on, so it
      // waits on the session exactly like a pending /connect does.
      if (response.status === "pending" || response.status === "mfa_submitted") {
        const answering = response.status === "mfa_submitted";
        const terminal = await pollFn({
          api,
          answeredMfaSessionId: answering ? sessionIdRef.current : null,
          answeredAttemptsRemaining: answering ? mfaRemainingRef.current : null,
        });
        if (terminal.session_id) {
          sessionIdRef.current = terminal.session_id;
        }
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
            prompt: mfaPrompt(
              terminal,
              terminal.message || "Enter the verification code from your provider to continue.",
            ),
          });
          emit("MFA_REQUIRED", {
            mfa_type: terminal.mfa_type ?? "otp",
            session_id: terminal.session_id ?? null,
          });
          telemetryRef.current?.mfaShown(terminal.mfa_type ?? "otp");
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
    [emit, failWith, pollFn, mfaPrompt],
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
      completedRef.current = true;
      clearCredentials();
      setMfaValues({});
      dispatch({
        type: "SUCCEED",
        payload: { summary: messages.success_message },
      });
      emit("CONNECTED", {
        job_id: resolved.job_id ?? null,
        public_token: publicToken,
        site: siteRef.current,
      });
    },
    [clearCredentials, emit, messages],
  );

  const onSubmitCredentials = useCallback(() => {
    const errors = validateSchemaValues(credentialSchema.fields, credentialValues);
    if (errors.length) {
      const map: Record<string, string> = {};
      for (const err of errors) map[err.field] = err.message;
      setCredentialErrors(map);
      for (const err of errors) {
        telemetryRef.current?.fieldError("credentials", err.field);
      }
      return;
    }
    setCredentialErrors({});
    const usernameValue = credentialValues.username ?? "";
    const passwordValue = credentialValues.password ?? "";
    // The password is encrypted and sent now; it has no reason to stay in
    // the page. A retry asks for it again.
    setCredentialValues((prev) => withoutSecrets(prev, credentialSchema.fields));
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
      for (const err of errors) {
        telemetryRef.current?.fieldError("mfa", err.field);
      }
      return;
    }
    setMfaErrors({});
    const codeValue = (mfaValues.code ?? "").trim();
    const awaitsApproval = mfaSchemaEntry.fields.length === 0;
    if (!codeValue && !awaitsApproval) {
      return;
    }
    dispatch({ type: "SUBMIT_MFA" });
    emit("MFA_SUBMITTED", { session_id: sessionId });
    telemetryRef.current?.mfaSubmitted();
    setMfaValues({});
    try {
      // A push-style prompt has nothing to type: the provider sees the
      // approval itself, so "I approved it" just waits on the session.
      const response: ConnectResponse = awaitsApproval
        ? { status: "mfa_submitted" }
        : await api.submitMfa({ sessionId, code: codeValue });
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
      {branding.logo ? (
        <div className="link-brand">
          <img className="link-brand__logo" src={branding.logo} alt="" aria-hidden="true" />
        </div>
      ) : null}
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
        {organizations.length === 0 && !searchError ? (
          <SkeletonRowList rows={4} />
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
                {organization.is_sandbox ? (
                  <span className="institution-item__badge">Sandbox</span>
                ) : organization.category_label ? (
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
        <form
          id="credentials-form"
          noValidate
          onSubmit={(event) => {
            // Enter in any field submits, and password managers see a real form.
            event.preventDefault();
            onSubmitCredentials();
          }}
        >
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
          <button id="connect-btn" type="submit">
            {credentialSchema.submit_label ?? messages.continue_cta}
          </button>
        </form>
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
        <SkeletonRowList rows={3} />
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
        <form
          id="mfa-form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            void onSubmitMfa();
          }}
        >
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
          <button id="mfa-submit-btn" type="submit">
            {mfaSchemaEntry.submit_label ?? messages.verify_cta}
          </button>
        </form>
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
            if (!apiRef.current && action !== "contact_support") {
              // No usable session behind this link: every way forward
              // leads back to the app.
              exitLink("invalid_link");
            } else if (action === "retry") {
              // Same provider again; the password was not kept.
              setCredentialErrors({});
              setCredentialValues((prev) => withoutSecrets(prev, credentialSchema.fields));
              dispatch({ type: "RETRY" });
            } else if (action === "back_to_picker") {
              clearCredentials();
              dispatch({ type: "BACK_TO_PICKER" });
            } else if (action === "contact_support") {
              emit("SUPPORT_REQUESTED", {
                error_code: state.error?.code ?? "internal_error",
              });
            } else if (action === "exit") {
              exitLink("user_exit");
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
