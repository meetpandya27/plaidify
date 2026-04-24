import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { applyTheme, resolveTheme } from "./i18n";
import "./design/tokens.css";
import "./design/primitives.css";
import "./link.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Missing #root container for Plaidify hosted link app.");
}

// Apply the `?theme=light|dark` override (falls back to
// `prefers-color-scheme` when absent).
applyTheme(resolveTheme(window.location.search));

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
