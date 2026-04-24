import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";

import { App } from "./App";

describe("App (scaffold)", () => {
  it("renders the placeholder heading", () => {
    const html = renderToString(<App />);
    expect(html).toContain("Plaidify Link");
  });
});
