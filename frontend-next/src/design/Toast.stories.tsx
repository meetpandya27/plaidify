import type { StoryDefault } from "@ladle/react";

import { Button } from "./Button";
import { ToastProvider, useToast } from "./Toast";

export default {
  title: "Primitives/Toast",
} satisfies StoryDefault;

function ToastDemo() {
  const { push } = useToast();
  return (
    <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
      <Button onClick={() => push({ message: "Saved", tone: "success" })}>
        Push success
      </Button>
      <Button variant="secondary" onClick={() => push({ message: "Heads up", tone: "warning" })}>
        Push warning
      </Button>
      <Button variant="danger" onClick={() => push({ message: "Something failed", tone: "danger" })}>
        Push danger
      </Button>
    </div>
  );
}

export const Playground = () => (
  <ToastProvider>
    <ToastDemo />
  </ToastProvider>
);
