import { describe, expect, it, vi } from "vitest";
import { renderToString } from "react-dom/server";
import { render, screen, fireEvent } from "@testing-library/react";

import { DynamicForm, validateSchemaValues } from "./DynamicForm";
import type { SchemaField } from "./api";

const fields: readonly SchemaField[] = [
  {
    id: "username",
    label: "Email",
    type: "email",
    required: true,
    pattern: "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$",
  },
  {
    id: "password",
    label: "Password",
    type: "password",
    required: true,
    secret: true,
    reveal: true,
    min_length: 6,
  },
];

describe("validateSchemaValues", () => {
  it("reports required fields as missing", () => {
    const errors = validateSchemaValues(fields, {});
    expect(errors.map((e) => e.field)).toEqual(["username", "password"]);
  });

  it("enforces regex patterns", () => {
    const errors = validateSchemaValues(fields, { username: "not-an-email", password: "hunter22" });
    expect(errors).toEqual([
      { field: "username", message: "Email is not in the expected format." },
    ]);
  });

  it("enforces min_length", () => {
    const errors = validateSchemaValues(fields, { username: "a@b.co", password: "hi" });
    expect(errors).toEqual([
      { field: "password", message: "Password must be at least 6 characters." },
    ]);
  });

  it("accepts valid values", () => {
    const errors = validateSchemaValues(fields, {
      username: "a@b.co",
      password: "hunter22",
    });
    expect(errors).toEqual([]);
  });
});

describe("DynamicForm", () => {
  it("renders prefixed ids with the right input types", () => {
    const html = renderToString(
      <DynamicForm fields={fields} values={{}} onChange={() => {}} />,
    );
    expect(html).toContain('id="link-username"');
    expect(html).toContain('id="link-password"');
    expect(html).toContain('type="email"');
    expect(html).toContain('type="password"');
  });

  it("fires onChange when the user types", () => {
    const onChange = vi.fn();
    render(<DynamicForm fields={fields} values={{}} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "a@b.co" } });
    expect(onChange).toHaveBeenCalledWith("username", "a@b.co");
  });

  it("toggles password reveal via the Show/Hide button", () => {
    render(<DynamicForm fields={fields} values={{ password: "secret" }} onChange={() => {}} />);
    const input = screen.getByLabelText("Password") as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.click(screen.getByRole("button", { name: /show password/i }));
    expect((screen.getByLabelText("Password") as HTMLInputElement).type).toBe("text");
  });

  it("renders inline errors with role=alert", () => {
    render(
      <DynamicForm
        fields={fields}
        values={{ username: "" }}
        errors={{ username: "Email is required." }}
        onChange={() => {}}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Email is required.");
  });
});
