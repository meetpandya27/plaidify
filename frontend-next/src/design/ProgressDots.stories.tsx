import type { StoryDefault } from "@ladle/react";

import { ProgressDots } from "./ProgressDots";

export default {
  title: "Primitives/ProgressDots",
} satisfies StoryDefault;

export const Default = () => <ProgressDots />;
export const Labelled = () => <ProgressDots label="Connecting to your bank" />;
