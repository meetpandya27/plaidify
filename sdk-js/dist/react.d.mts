import * as react from 'react';

type PlaidifyLinkEventName = "OPEN" | "CLOSE" | "INSTITUTION_SELECTED" | "CREDENTIALS_SUBMITTED" | "MFA_REQUIRED" | "MFA_SUBMITTED" | "CONNECTED" | "ERROR" | "EXIT" | "DONE" | "TELEMETRY" | "SUPPORT_REQUESTED";
interface PlaidifyLinkMfaDetails {
    mfa_type?: string;
    session_id?: string;
}
interface PlaidifyLinkExitDetails {
    reason?: string;
    error?: string;
    /** Error-taxonomy code of the last error, when the user exits from one. */
    error_code?: string;
}
interface PlaidifyLinkSuccessMetadata {
    job_id?: string;
    organization_id?: string;
    organization_name?: string;
    public_token?: string;
    site?: string;
}
interface PlaidifyLinkEventPayload extends PlaidifyLinkExitDetails, PlaidifyLinkMfaDetails {
    source?: "plaidify-link";
    event?: PlaidifyLinkEventName | string;
    job_id?: string;
    public_token?: string;
    organization_id?: string;
    organization_name?: string;
    site?: string;
    /** TELEMETRY only: which telemetry event (step_view, field_error, …). */
    name?: string;
    /** TELEMETRY only: the step the event concerns. */
    step?: string;
    /** TELEMETRY only: the form field that failed validation (never its value). */
    field?: string;
    /** TELEMETRY only: milliseconds since Link opened. */
    elapsed_ms?: number;
}
interface PlaidifyLinkConfig {
    /** Plaidify server URL. */
    serverUrl: string;
    /** Link token from POST /link/sessions or POST /link/sessions/public. */
    token: string;
    /** Theme overrides for the link UI. */
    theme?: LinkTheme;
    /** Called when link completes successfully with a public token, when one exists. */
    onSuccess?: (publicToken: string, metadata: PlaidifyLinkSuccessMetadata) => void;
    /** Called once when the user leaves Link without connecting. */
    onExit?: (details: PlaidifyLinkExitDetails) => void;
    /** Called on each link event, including recoverable ERRORs. */
    onEvent?: (event: PlaidifyLinkEventName | string, data: PlaidifyLinkEventPayload) => void;
    /** Called when the provider requires additional verification. */
    onMFA?: (details: PlaidifyLinkMfaDetails) => void;
}
interface LinkTheme {
    /** Buttons and focus rings, as a hex colour ("#0b8f73"). */
    accentColor?: string;
    /** Page background behind the Link card, as a hex colour. */
    bgColor?: string;
    /** Corner radius of the Link card, e.g. "24px" or "1.5rem". */
    borderRadius?: string;
    /**
     * Logo shown above every step, as a `data:image/…;base64,` URI of at
     * most 32 KB. The hosted page only loads images from itself and data:
     * URIs, so remote URLs are ignored.
     */
    logo?: string;
    fullscreenOnMobile?: boolean;
    mobileBreakpoint?: number;
}

interface UsePlaidifyLinkReturn {
    /** Open the link modal. Does nothing while it is already open. */
    open: () => void;
    /** Whether the link component is ready to open. */
    ready: boolean;
    /**
     * Current status of the link flow. "error" means the last event was a
     * recoverable ERROR: Link stays open on its retry screen.
     */
    status: "idle" | "loading" | "open" | "success" | "error";
    /** Close the link modal programmatically (reported as an exit). */
    close: () => void;
}
declare function usePlaidifyLink(config: PlaidifyLinkConfig): UsePlaidifyLinkReturn;
interface PlaidifyLinkProps extends PlaidifyLinkConfig {
    children: (props: UsePlaidifyLinkReturn) => React.ReactElement;
}
/**
 * Render-prop component for Plaidify Link.
 *
 * @example
 * ```tsx
 * <PlaidifyLink serverUrl="..." token={token} onSuccess={handleSuccess}>
 *   {({ open, ready }) => (
 *     <button onClick={open} disabled={!ready}>Connect</button>
 *   )}
 * </PlaidifyLink>
 * ```
 */
declare function PlaidifyLink({ children, ...config }: PlaidifyLinkProps): react.ReactElement<unknown, string | react.JSXElementConstructor<any>>;

export { PlaidifyLink, type PlaidifyLinkProps, type UsePlaidifyLinkReturn, usePlaidifyLink };
