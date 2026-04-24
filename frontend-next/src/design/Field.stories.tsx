import type { StoryDefault } from "@ladle/react";

import { Field } from "./Field";
import { Input } from "./Input";

export default {
  title: "Primitives/Field + Input",
} satisfies StoryDefault;

export const Basic = () => (
  <Field label="Username" help="Your bank username">
    <Input placeholder="you@example.com" />
  </Field>
);

export const Required = () => (
  <Field label="Password" required>
    <Input type="password" />
  </Field>
);

export const WithError = () => (
  <Field label="PIN" error="That code didn't match. Try again.">
    <Input type="text" defaultValue="0000" invalid />
  </Field>
);
