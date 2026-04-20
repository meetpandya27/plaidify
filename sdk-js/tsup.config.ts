import { defineConfig } from "tsup";

export default defineConfig([
  {
    entry: { index: "src/index.ts" },
    format: ["cjs", "esm"],
    dts: true,
    sourcemap: true,
    clean: true,
  },
  {
    entry: { react: "src/react.ts" },
    format: ["cjs", "esm"],
    dts: true,
    sourcemap: true,
    external: ["react", "react-dom"],
  },
  {
    entry: { "react-native": "src/react-native.ts" },
    format: ["cjs", "esm"],
    dts: true,
    sourcemap: true,
  },
]);
