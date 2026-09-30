/**
 * Embedder branding passed on the hosted-link URL by the SDKs
 * (`LinkTheme` in @plaidify/client, `PlaidifyLinkTheme` on iOS/Android):
 *
 *   ?accent=#0b8f73   primary buttons, focus rings
 *   ?bg=#eef5ff       page background behind the Link card
 *   ?radius=24px      corner radius of the Link card
 *   ?logo=data:image/png;base64,...   logo above every step
 *
 * Values come from a URL anyone can edit, so each one is checked against
 * a narrow grammar and dropped when it does not match; nothing here is
 * ever interpolated into markup or a stylesheet as free text. The logo
 * must be a data: URI because the page's Content-Security-Policy only
 * loads images from itself and `data:`.
 */

export interface Branding {
  readonly accent: string | null;
  readonly background: string | null;
  readonly radius: string | null;
  readonly logo: string | null;
}

export const NO_BRANDING: Branding = {
  accent: null,
  background: null,
  radius: null,
  logo: null,
};

const HEX_COLOR = /^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const LENGTH = /^(?:0|\d{1,3}(?:\.\d{1,2})?(?:px|rem|em))$/;
const LOGO_DATA_URI =
  /^data:image\/(?:png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/]+={0,2}$/;
/** Keeps a pasted logo from turning the link into a megabyte URL. */
export const MAX_LOGO_LENGTH = 32 * 1024;

function pick(params: URLSearchParams, key: string, valid: (value: string) => boolean): string | null {
  const value = params.get(key)?.trim();
  return value && valid(value) ? value : null;
}

export function resolveBranding(search?: string): Branding {
  if (!search) {
    return NO_BRANDING;
  }
  const params = new URLSearchParams(search);
  return {
    accent: pick(params, "accent", (v) => HEX_COLOR.test(v)),
    background: pick(params, "bg", (v) => HEX_COLOR.test(v)),
    radius: pick(params, "radius", (v) => LENGTH.test(v)),
    logo: pick(params, "logo", (v) => v.length <= MAX_LOGO_LENGTH && LOGO_DATA_URI.test(v)),
  };
}

/**
 * Apply the colour and shape overrides as CSS custom properties on the
 * document root. The logo is rendered by App.tsx.
 */
export function applyBranding(branding: Branding, root?: HTMLElement | null): void {
  const target = root ?? (typeof document !== "undefined" ? document.documentElement : null);
  if (!target) return;
  const style = target.style;
  if (branding.accent) {
    style.setProperty("--plaidify-color-accent-default", branding.accent);
    style.setProperty("--plaidify-color-accent-hover", `color-mix(in srgb, ${branding.accent} 85%, black)`);
    style.setProperty("--plaidify-color-accent-active", `color-mix(in srgb, ${branding.accent} 70%, black)`);
    style.setProperty("--plaidify-color-accent-subtle", `color-mix(in srgb, ${branding.accent} 14%, transparent)`);
    style.setProperty("--plaidify-color-border-focus", branding.accent);
  }
  if (branding.background) {
    style.setProperty("--plaidify-link-page-bg", branding.background);
  }
  if (branding.radius) {
    style.setProperty("--plaidify-radius-lg", branding.radius);
  }
}
