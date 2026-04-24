import type { StoryDefault } from "@ladle/react";

import { Button } from "./Button";

export default {
  title: "Primitives/Button",
} satisfies StoryDefault;

export const Primary = () => <Button>Continue</Button>;
export const Secondary = () => <Button variant="secondary">Cancel</Button>;
export const Ghost = () => <Button variant="ghost">Skip</Button>;
export const Danger = () => <Button variant="danger">Delete</Button>;
export const Block = () => <Button block>Connect my bank</Button>;
export const Disabled = () => <Button disabled>Connecting…</Button>;
