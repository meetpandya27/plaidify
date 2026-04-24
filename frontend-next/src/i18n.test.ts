import { describe, expect, it } from "vitest";
import {
  DEFAULT_LOCALE,
  MESSAGES,
  SUPPORTED_LOCALES,
  applyTheme,
  getMessages,
  resolveLocale,
  resolveTheme,
} from "./i18n";

describe("i18n", () => {
  it("ships a catalog for every supported locale", () => {
    for (const locale of SUPPORTED_LOCALES) {
      const messages = MESSAGES[locale];
      expect(messages).toBeDefined();
      expect(messages.step_select_heading.length).toBeGreaterThan(0);
      expect(messages.consent_bullets.length).toBe(3);
    }
  });

  it("en-US keeps the E2E contract strings stable", () => {
    expect(MESSAGES["en-US"].consent_bullets[2]).toBe(
      "Return a secure completion back to your app when verification finishes.",
    );
    expect(MESSAGES["en-US"].success_message).toContain("Return to your app");
    expect(MESSAGES["en-US"].public_token_label).toBe("PUBLIC TOKEN");
  });

  it("getMessages falls back to the default locale", () => {
    expect(getMessages("does-not-exist")).toBe(MESSAGES[DEFAULT_LOCALE]);
    expect(getMessages(undefined)).toBe(MESSAGES[DEFAULT_LOCALE]);
  });

  it("resolveLocale prefers ?locale= querystring", () => {
    expect(resolveLocale({ search: "?locale=fr-CA" })).toBe("fr-CA");
    expect(resolveLocale({ search: "?locale=xx-YY" })).toBe(DEFAULT_LOCALE);
  });

  it("resolveLocale falls back to Accept-Language header", () => {
    expect(
      resolveLocale({ acceptLanguage: "fr-CA,fr;q=0.9,en;q=0.5" }),
    ).toBe("fr-CA");
    expect(
      resolveLocale({ acceptLanguage: "de-DE,de;q=0.9" }),
    ).toBe(DEFAULT_LOCALE);
  });

  it("resolveLocale matches by language prefix", () => {
    expect(resolveLocale({ navigatorLanguages: ["fr"] })).toBe("fr-CA");
    expect(resolveLocale({ navigatorLanguages: ["en"] })).toBe("en-US");
  });

  it("fr-CA translates consent bullets", () => {
    const fr = MESSAGES["fr-CA"];
    expect(fr.consent_bullets[0]).toContain("fournisseur");
    expect(fr.public_token_label).toBe("JETON PUBLIC");
  });

  it("resolveTheme parses ?theme=", () => {
    expect(resolveTheme("?theme=dark")).toBe("dark");
    expect(resolveTheme("?theme=light")).toBe("light");
    expect(resolveTheme("?foo=bar")).toBe("system");
    expect(resolveTheme("")).toBe("system");
    expect(resolveTheme(undefined)).toBe("system");
  });

  it("applyTheme sets data-theme on the target element", () => {
    const root = document.createElement("html");
    applyTheme("dark", root);
    expect(root.getAttribute("data-theme")).toBe("dark");
    applyTheme("light", root);
    expect(root.getAttribute("data-theme")).toBe("light");
    applyTheme("system", root);
    expect(root.hasAttribute("data-theme")).toBe(false);
  });
});
