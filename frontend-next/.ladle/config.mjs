/** @type {import("@ladle/react").UserConfig} */
export default {
  stories: "src/**/*.stories.{ts,tsx}",
  defaultStory: "primitives-button--primary",
  outDir: "ladle-build",
  addons: {
    theme: {
      enabled: true,
      defaultState: "light",
    },
    mode: {
      enabled: false,
    },
  },
};
