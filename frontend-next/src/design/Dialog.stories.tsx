import { useState } from "react";
import type { StoryDefault } from "@ladle/react";

import { Button } from "./Button";
import { Dialog } from "./Dialog";

export default {
  title: "Primitives/Dialog",
} satisfies StoryDefault;

export const Confirmation = () => {
  const [open, setOpen] = useState(true);
  return (
    <>
      <Button onClick={() => setOpen(true)}>Open dialog</Button>
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title="Leave Plaidify?"
        description="You'll need to restart the connection if you exit now."
      >
        <div style={{ display: "flex", gap: 12, justifyContent: "flex-end" }}>
          <Button variant="secondary" onClick={() => setOpen(false)}>
            Stay
          </Button>
          <Button variant="danger" onClick={() => setOpen(false)}>
            Exit
          </Button>
        </div>
      </Dialog>
    </>
  );
};
