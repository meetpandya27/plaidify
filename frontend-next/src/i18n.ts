/**
 * Tiny message catalog + locale negotiation for the hosted-link app
 * (issue #57). We deliberately hand-roll this rather than pulling in
 * a full i18n framework — the string surface is small, the catalogs
 * are shipped statically in the bundle, and we keep one source of
 * truth (`en-US`) to satisfy the E2E DOM contract.
 *
 * Locale negotiation order (first match wins):
 *   1. \`?locale=xx-YY\` querystring
 *   2. \`Accept-Language\` (when provided, e.g. SSR or tests)
 *   3. \`navigator.language(s)\`
 *   4. Default \`en-US\`
 */

export type Locale = "en-US" | "en-CA" | "fr-CA";

export const SUPPORTED_LOCALES: readonly Locale[] = ["en-US", "en-CA", "fr-CA"];
export const DEFAULT_LOCALE: Locale = "en-US";

export interface Messages {
  readonly step_select_heading: string;
  readonly step_credentials_heading: string;
  readonly step_connecting_heading: string;
  readonly step_connecting_body: string;
  readonly step_mfa_heading: string;
  readonly step_success_heading: string;
  readonly step_error_heading: string;
  readonly search_label: string;
  readonly search_placeholder: string;
  readonly consent_bullets: readonly string[];
  readonly success_message: string;
  readonly public_token_label: string;
  readonly retry_cta: string;
  readonly continue_cta: string;
  readonly verify_cta: string;
  readonly live_select: string;
  readonly live_credentials: string;
  readonly live_connecting: string;
  readonly live_mfa: string;
  readonly live_success: string;
  readonly live_error: string;
}

// en-US is the source of truth. E2E assertions rely on exact English
// copy for the consent bullet ("Return a secure completion back to
// your app when verification finishes.") and the success message
// ("Return to your app") — keep those wordings stable here.
const EN_US: Messages = {
  step_select_heading: "Select your provider",
  step_credentials_heading: "Enter your credentials",
  step_connecting_heading: "Connecting",
  step_connecting_body: "Creating your secure session…",
  step_mfa_heading: "Finish verification",
  step_success_heading: "Connection successful",
  step_error_heading: "Connection failed",
  search_label: "Search providers",
  search_placeholder: "Search providers",
  consent_bullets: [
    "Open a secure browser session with the provider you choose.",
    "Encrypt your sign-in details before they leave this window.",
    "Return a secure completion back to your app when verification finishes.",
  ],
  success_message:
    "Your secure connection is complete. Return to your app to finish setup.",
  public_token_label: "PUBLIC TOKEN",
  retry_cta: "Try again",
  continue_cta: "Continue",
  verify_cta: "Verify and continue",
  live_select: "Choose your provider to get started.",
  live_credentials: "Enter your credentials for the selected provider.",
  live_connecting: "Connecting to your provider.",
  live_mfa: "Additional verification required.",
  live_success: "Connection successful.",
  live_error: "Connection failed.",
};

// en-CA currently mirrors en-US; kept as a distinct entry so future
// Canadian-English tweaks (e.g. "organisation") can diverge cleanly.
const EN_CA: Messages = EN_US;

const FR_CA: Messages = {
  step_select_heading: "Choisissez votre fournisseur",
  step_credentials_heading: "Entrez vos identifiants",
  step_connecting_heading: "Connexion en cours",
  step_connecting_body: "Création de votre session sécurisée…",
  step_mfa_heading: "Terminez la vérification",
  step_success_heading: "Connexion réussie",
  step_error_heading: "Échec de la connexion",
  search_label: "Rechercher des fournisseurs",
  search_placeholder: "Rechercher des fournisseurs",
  consent_bullets: [
    "Ouvrir une session de navigation sécurisée avec le fournisseur choisi.",
    "Chiffrer vos identifiants avant qu’ils ne quittent cette fenêtre.",
    "Renvoyer une confirmation sécurisée à votre application une fois la vérification terminée.",
  ],
  success_message:
    "Votre connexion sécurisée est établie. Retournez à votre application pour terminer la configuration.",
  public_token_label: "JETON PUBLIC",
  retry_cta: "Réessayer",
  continue_cta: "Continuer",
  verify_cta: "Vérifier et continuer",
  live_select: "Choisissez votre fournisseur pour commencer.",
  live_credentials: "Entrez vos identifiants pour le fournisseur sélectionné.",
  live_connecting: "Connexion à votre fournisseur en cours.",
  live_mfa: "Vérification supplémentaire requise.",
  live_success: "Connexion réussie.",
  live_error: "Échec de la connexion.",
};

export const MESSAGES: Readonly<Record<Locale, Messages>> = {
  "en-US": EN_US,
  "en-CA": EN_CA,
  "fr-CA": FR_CA,
};

export function getMessages(locale: Locale | string | null | undefined): Messages {
  if (locale && (locale as Locale) in MESSAGES) {
    return MESSAGES[locale as Locale];
  }
  return MESSAGES[DEFAULT_LOCALE];
}

/**
 * Parse an \`Accept-Language\` style header or \`navigator.languages\`
 * array into the first supported locale. Matches on exact locale,
 * then on language prefix (\`fr\` → \`fr-CA\`).
 */
function matchLocale(candidates: readonly string[]): Locale | null {
  for (const raw of candidates) {
    if (!raw) continue;
    const tag = raw.trim();
    const exact = SUPPORTED_LOCALES.find(
      (l) => l.toLowerCase() === tag.toLowerCase(),
    );
    if (exact) return exact;
    const prefix = tag.split("-")[0]?.toLowerCase();
    if (!prefix) continue;
    const byPrefix = SUPPORTED_LOCALES.find(
      (l) => l.split("-")[0].toLowerCase() === prefix,
    );
    if (byPrefix) return byPrefix;
  }
  return null;
}

function parseAcceptLanguage(header: string): string[] {
  return header
    .split(",")
    .map((entry) => {
      const [tag, qPart] = entry.split(";");
      const q = qPart
        ? parseFloat(qPart.split("=")[1] ?? "1") || 0
        : 1;
      return { tag: (tag || "").trim(), q };
    })
    .filter((e) => e.tag.length > 0)
    .sort((a, b) => b.q - a.q)
    .map((e) => e.tag);
}

export interface LocaleResolutionInput {
  readonly search?: string;
  readonly acceptLanguage?: string;
  readonly navigatorLanguages?: readonly string[];
}

export function resolveLocale(input: LocaleResolutionInput = {}): Locale {
  if (input.search) {
    const params = new URLSearchParams(input.search);
    const explicit = params.get("locale");
    if (explicit) {
      const matched = matchLocale([explicit]);
      if (matched) return matched;
    }
  }
  if (input.acceptLanguage) {
    const matched = matchLocale(parseAcceptLanguage(input.acceptLanguage));
    if (matched) return matched;
  }
  if (input.navigatorLanguages && input.navigatorLanguages.length > 0) {
    const matched = matchLocale(input.navigatorLanguages);
    if (matched) return matched;
  }
  return DEFAULT_LOCALE;
}

export type Theme = "light" | "dark" | "system";

export function resolveTheme(search?: string): Theme {
  if (!search) return "system";
  const params = new URLSearchParams(search);
  const value = params.get("theme");
  if (value === "light" || value === "dark") return value;
  return "system";
}

/**
 * Apply the requested theme to the document root. When \`system\`, we
 * clear the override so the CSS \`@media (prefers-color-scheme)\`
 * fallback kicks in.
 */
export function applyTheme(theme: Theme, root?: HTMLElement | null): void {
  const target = root ?? (typeof document !== "undefined" ? document.documentElement : null);
  if (!target) return;
  if (theme === "system") {
    target.removeAttribute("data-theme");
  } else {
    target.setAttribute("data-theme", theme);
  }
}
