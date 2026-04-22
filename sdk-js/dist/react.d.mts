import * as react from 'react';

type PlaidifyLinkEventName = "OPEN" | "CLOSE" | "INSTITUTION_SELECTED" | "CREDENTIALS_SUBMITTED" | "MFA_REQUIRED" | "MFA_SUBMITTED" | "CONNECTED" | "ERROR" | "EXIT" | "DONE";
interface PlaidifyLinkMfaDetails {
    mfa_type?: string;
    session_id?: string;
}
interface PlaidifyLinkExitDetails {
    reason?: string;
    error?: string;
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
    /** Called when the user exits the link flow. */
    onExit?: (details: PlaidifyLinkExitDetails) => void;
    /** Called on each link event. */
    onEvent?: (event: PlaidifyLinkEventName | string, data: PlaidifyLinkEventPayload) => void;
    /** Called when the provider requires additional verification. */
    onMFA?: (details: PlaidifyLinkMfaDetails) => void;
}
interface LinkTheme {
    accentColor?: string;
    bgColor?: string;
    borderRadius?: string;
    logo?: string;
    fullscreenOnMobile?: boolean;
    mobileBreakpoint?: number;
}

interface UsePlaidifyLinkReturn {
    /** Open the link modal. */
    open: () => void;
    /** Whether the link component is ready to open. */
    ready: boolean;
    /** Current status of the link flow. */
    status: "idle" | "loading" | "open" | "success" | "error";
    /** Close the link modal programmatically. */
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
declare function PlaidifyLink({ children, ...config }: PlaidifyLinkProps): react.ReactElement<any, string | react.JSXElementConstructor<any>>;

export { PlaidifyLink, type PlaidifyLinkProps, type UsePlaidifyLinkReturn, usePlaidifyLink };
