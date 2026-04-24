import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build output is consumed by FastAPI as a static bundle.
// We publish a single-page app; the Plaidify backend routes /link to
// this page and injects the session state via the existing
// /link/sessions/{token}/status API.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/link/sessions": "http://127.0.0.1:8000",
      "/organizations": "http://127.0.0.1:8000",
    },
  },
});
