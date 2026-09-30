/**
 * The hosted Link page URL, shared by the client, the React hook and the
 * React Native helpers so they all speak the parameters the page reads
 * (frontend-next/src/config.ts and branding.ts).
 */

import type { HostedLinkUrlOptions } from "./types";

/**
 * `url` without its trailing slashes. A loop rather than `/\/+$/`, which
 * backtracks quadratically on a long run of slashes.
 */
export function trimTrailingSlashes(url: string): string {
  let end = url.length;
  while (end > 0 && url.charCodeAt(end - 1) === 47 /* "/" */) {
    end -= 1;
  }
  return url.slice(0, end);
}

export function buildHostedLinkUrl(
  serverUrl: string,
  linkToken: string,
  options: HostedLinkUrlOptions = {},
  /** Resolves a relative `serverUrl` (e.g. `window.location.href`). */
  base?: string,
): string {
  const url = new URL(`${trimTrailingSlashes(serverUrl)}/link`, base);
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
