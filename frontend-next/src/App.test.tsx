import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToString } from "react-dom/server";

import { App } from "./App";
import { initialFlowState, type Institution } from "./state";
import type { Organization } from "./api";

const hydro: Organization = {
  organization_id: "org-hydro",
  site: "hydro_one",
  name: "Hydro One",
};
const institutionHydro: Institution = {
  site: "hydro_one",
  name: "Hydro One",
};

describe("App (SSR smoke)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the picker with a seeded organization list", () => {
    const html = renderToString(
      <App
        seedInstitutions={[hydro]}
        apiFactory={() =>
          ({ getStatus: async () => ({ status: "pending" }) } as unknown as ReturnType<
            NonNullable<Parameters<typeof App>[0]>["apiFactory"] & object
          >)
        }
        buildEventDelivery={() => null}
      />,
    );
    expect(html).toContain('id="step-select"');
    expect(html).toContain('class="link-step active"');
    expect(html).toContain('id="institution-search"');
    expect(html).toContain('class="institution-item"');
    expect(html).toContain("Hydro One");
    expect(html).toContain('id="consent-list"');
    expect(html).toContain(
      "Return a secure completion back to your app when verification finishes.",
    );
  });

  it("renders the credentials step with the selected provider", () => {
    const html = renderToString(
      <App
        initialState={{ ...initialFlowState, step: "credentials", institution: institutionHydro }}
        buildEventDelivery={() => null}
      />,
    );
    expect(html).toContain('id="step-credentials"');
    expect(html).toContain('id="provider-name"');
    expect(html).toContain("Hydro One");
    expect(html).toContain('id="link-username"');
    expect(html).toContain('id="link-password"');
    expect(html).toContain('id="connect-btn"');
  });

  it("renders the success step with the PUBLIC TOKEN reference", () => {
    const html = renderToString(
      <App
        initialState={{
          ...initialFlowState,
          step: "success",
          success: {
            accessToken: "public-abc123",
            summary: "Your secure connection is complete. Return to your app to finish setup.",
          },
        }}
        buildEventDelivery={() => null}
      />,
    );
    expect(html).toContain('id="step-success"');
    expect(html).toContain('class="link-step active"');
    expect(html).toContain("PUBLIC TOKEN");
    expect(html).toContain("public-abc123");
    expect(html).toContain("Return to your app");
  });
});
