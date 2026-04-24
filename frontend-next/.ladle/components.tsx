import type { GlobalProvider } from "@ladle/react";
import { useEffect } from "react";

import "../src/design/tokens.css";
import "../src/design/primitives.css";

/**
 * Apply Ladle's theme addon to the document root so the Plaidify
 * tokens swap between light and dark values live.
 */
export const Provider: GlobalProvider = ({ children, globalState }) => {
  useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.dataset.theme = globalState.theme;
  }, [globalState.theme]);

  return (
    <div
      style={{
        background: "var(--plaidify-color-bg-canvas)",
        color: "var(--plaidify-color-fg-default)",
        fontFamily: "var(--plaidify-font-family-sans)",
        padding: "var(--plaidify-space-6)",
        minHeight: "100vh",
      }}
    >
      {children}
    </div>
  );
};
