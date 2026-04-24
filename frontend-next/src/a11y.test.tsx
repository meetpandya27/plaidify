/**
 * Automated accessibility checks (issue #56). Uses axe-core against
 * each rendered hosted-link step to catch regressions of WCAG 2.1 AA
 * issues (missing labels, ARIA misuse, etc.). Color contrast is
 * audited manually since jsdom cannot compute layout.
 */
import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import axe, { type AxeResults } from "axe-core";

import { App } from "./App";
import { initialFlowState, type Institution } from "./state";
import type { Organization } from "./api";

const hydro: Organization = {
  organization_id: "org-hydro",
  site: "hydro_one",
  name: "Hydro One",
  logo_monogram: "HO",
  primary_color: "#1f6f43",
  secondary_color: "#e7f4ec",
  accent_color: "#f2c14e",
  hint_copy: "Use your utility portal sign-in.",
  auth_style: "username_password",
};

const institutionHydro: Institution = {
  site: "hydro_one",
  name: "Hydro One",
  primary_color: "#1f6f43",
  hint_copy: "Use your utility portal sign-in.",
  auth_style: "username_password",
};

function mountHtml(html: string): HTMLElement {
  const container = document.createElement("div");
  container.innerHTML = html;
  document.body.appendChild(container);
  return container;
}

async function runAxe(container: HTMLElement): Promise<AxeResults> {
  return axe.run(container, {
    // jsdom cannot reason about layout, so skip rules that depend on it.
    rules: {
      "color-contrast": { enabled: false },
      "aria-hidden-focus": { enabled: false },
      region: { enabled: false },
    },
  });
}

function criticalViolations(results: AxeResults) {
  return results.violations.filter(
    (v) => v.impact === "critical" || v.impact === "serious",
  );
}

describe("hosted-link accessibility (axe-core)", () => {
  it("provider-picker step is clean", async () => {
    const html = renderToString(
      <App seedInstitutions={[hydro]} buildEventDelivery={() => null} />,
    );
    const container = mountHtml(html);
    const results = await runAxe(container);
    const critical = criticalViolations(results);
    expect(
      critical,
      critical.map((v) => `${v.id}: ${v.description}`).join("\n"),
    ).toHaveLength(0);
    container.remove();
  });

  it("credentials step is clean", async () => {
    const html = renderToString(
      <App
        initialState={{
          ...initialFlowState,
          step: "credentials",
          institution: institutionHydro,
        }}
        buildEventDelivery={() => null}
      />,
    );
    const container = mountHtml(html);
    const results = await runAxe(container);
    const critical = criticalViolations(results);
    expect(
      critical,
      critical.map((v) => `${v.id}: ${v.description}`).join("\n"),
    ).toHaveLength(0);
    container.remove();
  });

  it("error step is clean", async () => {
    const html = renderToString(
      <App
        initialState={{
          ...initialFlowState,
          step: "error",
          error: { message: "nope", code: "invalid_credentials" },
        }}
        buildEventDelivery={() => null}
      />,
    );
    const container = mountHtml(html);
    const results = await runAxe(container);
    const critical = criticalViolations(results);
    expect(
      critical,
      critical.map((v) => `${v.id}: ${v.description}`).join("\n"),
    ).toHaveLength(0);
    container.remove();
  });

  it("ships an sr-only polite live region for step transitions", () => {
    const html = renderToString(
      <App seedInstitutions={[hydro]} buildEventDelivery={() => null} />,
    );
    expect(html).toContain('id="link-live-region"');
    expect(html).toContain('role="status"');
    expect(html).toContain('aria-live="polite"');
  });

  it("error region announces via aria-live=assertive", () => {
    const html = renderToString(
      <App
        initialState={{
          ...initialFlowState,
          step: "error",
          error: { message: "Network offline", code: "network_error" },
        }}
        buildEventDelivery={() => null}
      />,
    );
    expect(html).toContain('aria-live="assertive"');
    expect(html).toContain('data-error-code="network_error"');
  });
});
