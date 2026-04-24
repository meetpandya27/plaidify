import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";

import { App } from "./App";
import { initialFlowState, type Institution } from "./state";

const hydro: Institution = { site: "hydro_one", name: "Hydro One" };

describe("App", () => {
  it("renders the institution picker with the E2E-required DOM ids", () => {
    const html = renderToString(<App institutions={[hydro]} />);

    expect(html).toContain('id="step-select"');
    expect(html).toContain('class="link-step active"');
    expect(html).toContain('id="institution-search"');
    expect(html).toContain('class="institution-item"');
    expect(html).toContain("Hydro One");
  });

  it("renders the credentials step structure when pre-seeded", () => {
    const html = renderToString(
      <App
        institutions={[hydro]}
        initialState={{
          ...initialFlowState,
          step: "credentials",
          institution: hydro,
        }}
      />,
    );

    expect(html).toContain('id="step-credentials"');
    expect(html).toContain('class="link-step active"');
    expect(html).toContain('id="provider-name"');
    expect(html).toContain("Hydro One");
    expect(html).toContain('id="link-username"');
    expect(html).toContain('id="link-password"');
    expect(html).toContain('id="connect-btn"');
    expect(html).toContain('id="consent-list"');
  });

  it("renders the success step structure when pre-seeded", () => {
    const html = renderToString(
      <App
        initialState={{
          ...initialFlowState,
          step: "success",
          success: { accessToken: "tok_live_abc", summary: "Account linked." },
        }}
      />,
    );

    expect(html).toContain('id="step-success"');
    expect(html).toContain('class="link-step active"');
    expect(html).toContain('id="success-message"');
    expect(html).toContain("Account linked.");
    expect(html).toContain('id="access-token-display"');
    expect(html).toContain("tok_live_abc");
  });
});
