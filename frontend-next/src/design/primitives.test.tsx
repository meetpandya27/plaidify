import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { Field } from "./Field";
import { Input } from "./Input";
import { ProgressDots } from "./ProgressDots";
import { ToastProvider, useToast } from "./Toast";

describe("Button", () => {
  it("renders a primary button by default", () => {
    render(<Button>Connect</Button>);
    const button = screen.getByRole("button", { name: "Connect" });
    expect(button.className).toContain("plaidify-button");
    expect(button.className).toContain("plaidify-button--primary");
    expect(button.getAttribute("type")).toBe("button");
  });

  it("applies the variant and block modifiers", () => {
    render(
      <Button variant="danger" block>
        Delete
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Delete" });
    expect(button.className).toContain("plaidify-button--danger");
    expect(button.className).toContain("plaidify-button--block");
  });

  it("fires onClick when clicked", async () => {
    const onClick = vi.fn();
    const { container } = render(<Button onClick={onClick}>Go</Button>);
    const button = container.querySelector("button")!;
    button.click();
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe("Input", () => {
  it("reflects the invalid flag in aria-invalid and the class list", () => {
    render(<Input invalid defaultValue="bad" />);
    const input = screen.getByDisplayValue("bad");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.className).toContain("plaidify-input--invalid");
  });
});

describe("Field", () => {
  it("wires label, help and error to the control for screen readers", () => {
    render(
      <Field label="Username" help="Enter your bank username" error="Required" required>
        <Input />
      </Field>,
    );
    const input = screen.getByLabelText(/Username/);
    const describedBy = input.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(describedBy!.split(" ").length).toBe(2);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.getAttribute("aria-required")).toBe("true");
    expect(screen.getByRole("alert").textContent).toBe("Required");
  });
});

describe("Dialog", () => {
  it("returns null when closed", () => {
    const { container } = render(
      <Dialog open={false} title="Hi">
        body
      </Dialog>,
    );
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });

  it("renders with accessible metadata when open", () => {
    render(
      <Dialog open title="Confirm exit" description="Are you sure?">
        <button>Stay</button>
      </Dialog>,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(screen.getByText("Confirm exit")).toBeInTheDocument();
    expect(screen.getByText("Are you sure?")).toBeInTheDocument();
  });
});

describe("ProgressDots", () => {
  it("exposes a live region label", () => {
    render(<ProgressDots label="Connecting" />);
    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-label")).toBe("Connecting");
    expect(status.querySelectorAll(".plaidify-progress-dots__dot").length).toBe(3);
  });
});

describe("ToastProvider", () => {
  function Producer({ onReady }: { onReady: (push: ReturnType<typeof useToast>["push"]) => void }) {
    const { push } = useToast();
    onReady(push);
    return null;
  }

  it("throws when useToast is called outside the provider", () => {
    function Orphan() {
      useToast();
      return null;
    }
    expect(() => render(<Orphan />)).toThrow(/ToastProvider/);
  });

  it("renders toasts pushed through the context", () => {
    let pushFn: ReturnType<typeof useToast>["push"] | null = null;
    render(
      <ToastProvider defaultDurationMs={0}>
        <Producer onReady={(p) => (pushFn = p)} />
      </ToastProvider>,
    );
    expect(pushFn).not.toBeNull();
    act(() => {
      pushFn!({ message: "Saved", tone: "success" });
    });
    const toast = screen.getByText("Saved");
    expect(toast.className).toContain("plaidify-toast--success");
  });
});
