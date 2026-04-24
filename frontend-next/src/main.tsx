import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./design/tokens.css";
import "./design/primitives.css";
import "./link.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Missing #root container for Plaidify hosted link app.");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
