/**
 * The hosted Link page URL, shared by the client, the React hook and the
 * React Native helpers so they all speak the parameters the page reads
 * (frontend-next/src/config.ts and branding.ts).
 */

import type { HostedLinkUrlOptions } from "./types";

export function buildHostedLinkUrl(
  serverUrl: string,
  linkToken: string,
  options: HostedLinkUrlOptions = {},
  /** Resolves a relative `serverUrl` (e.g. `window.location.href`). */
  base?: string,
): string {
  const url = new URL(`${serverUrl.replace(/\/+$/, "")}/link`, base);
  url.searchParams.set("token", linkToken);

  if (options.origin) {
    url.searchParams.set("origin", options.origin);
  }

  const theme = options.theme;
  if (theme?.accentColor) {
    url.searchParams.set("accent", theme.accentColor);
  }
  if (theme?.bgColor) {
    url.searchParams.set("bg", theme.bgColor);
  }
  if (theme?.borderRadius) {
    url.searchParams.set("radius", theme.borderRadius);
  }
  if (theme?.logo) {
    url.searchParams.set("logo", theme.logo);
  }

  return url.toString();
}
