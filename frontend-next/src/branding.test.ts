import { describe, expect, it } from "vitest";

import { MAX_LOGO_LENGTH, NO_BRANDING, applyBranding, resolveBranding } from "./branding";

const LOGO = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

describe("resolveBranding", () => {
  it("reads the parameters the SDKs send", () => {
    const search = new URLSearchParams({
      token: "tok",
      accent: "#0b8f73",
      bg: "#EEF5FF",
      radius: "24px",
      logo: LOGO,
    }).toString();
    expect(resolveBranding(`?${search}`)).toEqual({
      accent: "#0b8f73",
      background: "#EEF5FF",
      radius: "24px",
      logo: LOGO,
    });
  });

  it("returns nothing without parameters", () => {
    expect(resolveBranding("")).toBe(NO_BRANDING);
    expect(resolveBranding(undefined)).toBe(NO_BRANDING);
    expect(resolveBranding("?token=tok")).toEqual(NO_BRANDING);
  });

  it("drops values outside the expected grammar", () => {
    const hostile = new URLSearchParams({
      accent: "red;background:url(https://evil.example/x)",
      bg: "expression(alert(1))",
      radius: "calc(100vw)",
      logo: "https://evil.example/logo.png",
    }).toString();
    expect(resolveBranding(`?${hostile}`)).toEqual(NO_BRANDING);

    const script = new URLSearchParams({ logo: "data:text/html;base64,PHNjcmlwdD4=" }).toString();
    expect(resolveBranding(`?${script}`).logo).toBeNull();
  });

  it("refuses an oversized logo", () => {
    const huge = `data:image/png;base64,${"A".repeat(MAX_LOGO_LENGTH)}`;
    expect(resolveBranding(`?${new URLSearchParams({ logo: huge })}`).logo).toBeNull();
  });
});

describe("applyBranding", () => {
  it("sets the design tokens the page styles read", () => {
    const root = document.createElement("html");
    applyBranding({ accent: "#0b8f73", background: "#eef5ff", radius: "1.5rem", logo: null }, root);
    expect(root.style.getPropertyValue("--plaidify-color-accent-default")).toBe("#0b8f73");
    expect(root.style.getPropertyValue("--plaidify-color-border-focus")).toBe("#0b8f73");
    expect(root.style.getPropertyValue("--plaidify-link-page-bg")).toBe("#eef5ff");
    expect(root.style.getPropertyValue("--plaidify-radius-lg")).toBe("1.5rem");
  });

  it("leaves the defaults alone when nothing was requested", () => {
    const root = document.createElement("html");
    applyBranding(NO_BRANDING, root);
    expect(root.getAttribute("style")).toBeNull();
  });
});
