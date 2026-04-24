/**
 * DynamicForm — renders a connector-provided field schema as HTML inputs
 * with inline validation. The hosted-link flow uses this for both the
 * credentials step and the MFA step. Design goals:
 *
 *  1. Backwards-compatible DOM IDs: each field gets `id="link-<field.id>"`
 *     and `name="<field.id>"`. The existing Playwright E2E relies on
 *     `#link-username`, `#link-password`, `#mfa-code`.
 *  2. Inline validation: `required`, `pattern`, `min_length`, `max_length`
 *     are enforced in JS so error states show up below the field before
 *     the request is sent. HTML constraint attributes also double as a
 *     fallback when JS is disabled.
 *  3. Password reveal: fields with `secret: true` and `reveal: true`
 *     render a toggle that flips the input between `type="password"` and
 *     `type="text"`.
 */
import { useId, useMemo, useState } from "react";

import type { SchemaField } from "./api";

export interface FieldValidationError {
  readonly field: string;
  readonly message: string;
}

export function validateSchemaValues(
  fields: readonly SchemaField[],
  values: Readonly<Record<string, string>>,
): FieldValidationError[] {
  const errors: FieldValidationError[] = [];
  for (const field of fields) {
    const raw = values[field.id] ?? "";
    const value = raw.trim();
    if (field.required && !value) {
      errors.push({ field: field.id, message: `${field.label} is required.` });
      continue;
    }
    if (!value) continue;
    if (field.min_length !== undefined && value.length < field.min_length) {
      errors.push({
        field: field.id,
        message: `${field.label} must be at least ${field.min_length} characters.`,
      });
      continue;
    }
    if (field.max_length !== undefined && value.length > field.max_length) {
      errors.push({
        field: field.id,
        message: `${field.label} must be at most ${field.max_length} characters.`,
      });
      continue;
    }
    if (field.pattern) {
      try {
        const re = new RegExp(field.pattern);
        if (!re.test(value)) {
          errors.push({
            field: field.id,
            message: `${field.label} is not in the expected format.`,
          });
        }
      } catch {
        // Bad pattern from the connector — ignore rather than block the user.
      }
    }
  }
  return errors;
}

export interface DynamicFormProps {
  readonly idPrefix?: string;
  readonly fields: readonly SchemaField[];
  readonly values: Readonly<Record<string, string>>;
  readonly errors?: Readonly<Record<string, string>>;
  readonly onChange: (id: string, value: string) => void;
  readonly onBlur?: (id: string) => void;
  readonly disabled?: boolean;
}

export function DynamicForm({
  idPrefix = "link",
  fields,
  values,
  errors,
  onChange,
  onBlur,
  disabled,
}: DynamicFormProps) {
  const [revealed, setRevealed] = useState<Readonly<Record<string, boolean>>>({});
  const describeBaseId = useId();

  const resolvedFields = useMemo(() => fields, [fields]);

  return (
    <div className="dynamic-form">
      {resolvedFields.map((field) => {
        const domId = `${idPrefix}-${field.id}`;
        const helpId = `${describeBaseId}-${field.id}-help`;
        const errorId = `${describeBaseId}-${field.id}-error`;
        const fieldError = errors?.[field.id];
        const isSecret = field.type === "password" || field.secret;
        const revealToggle = isSecret && field.reveal !== false;
        const revealed_ = revealed[field.id] === true;
        const resolvedType = isSecret && revealed_ ? "text" : field.type;

        return (
          <div className="dynamic-form__field" key={field.id}>
            <label htmlFor={domId}>{field.label}</label>
            <div className="dynamic-form__control">
              <input
                id={domId}
                name={field.id}
                type={resolvedType}
                autoComplete={field.autocomplete}
                inputMode={field.inputmode}
                placeholder={field.placeholder}
                required={field.required}
                minLength={field.min_length}
                maxLength={field.max_length}
                pattern={field.pattern}
                value={values[field.id] ?? ""}
                aria-invalid={fieldError ? true : undefined}
                aria-describedby={
                  [fieldError ? errorId : null, field.help_text ? helpId : null]
                    .filter(Boolean)
                    .join(" ") || undefined
                }
                disabled={disabled}
                onChange={(e) => onChange(field.id, e.target.value)}
                onBlur={() => onBlur?.(field.id)}
              />
              {revealToggle ? (
                <button
                  type="button"
                  className="dynamic-form__reveal"
                  aria-pressed={revealed_}
                  aria-label={revealed_ ? `Hide ${field.label}` : `Show ${field.label}`}
                  onClick={() =>
                    setRevealed((prev) => ({ ...prev, [field.id]: !prev[field.id] }))
                  }
                  disabled={disabled}
                >
                  {revealed_ ? "Hide" : "Show"}
                </button>
              ) : null}
            </div>
            {field.help_text ? (
              <p id={helpId} className="dynamic-form__help">
                {field.help_text}
              </p>
            ) : null}
            {fieldError ? (
              <p id={errorId} className="dynamic-form__error" role="alert">
                {fieldError}
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
