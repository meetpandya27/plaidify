import * as react from 'react';

interface PlaidifyLinkConfig {
    /** Plaidify server URL. */
    serverUrl: string;
    /** Link token from POST /link/create. */
    token: string;
    /** Theme overrides for the link UI. */
    theme?: LinkTheme;
    /** Called when link completes successfully. */
    onSuccess?: (publicToken: string, metadata: Record<string, unknown>) => void;
    /** Called when the user exits the link flow. */
    onExit?: (error?: string) => void;
    /** Called on each link event. */
    onEvent?: (event: string, data: Record<string, unknown>) => void;
}
interface LinkTheme {
    accentColor?: string;
    bgColor?: string;
    borderRadius?: string;
    logo?: string;
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
