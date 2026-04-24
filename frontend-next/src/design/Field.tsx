import { useId } from "react";
import type { ReactElement, ReactNode } from "react";

export interface FieldProps {
  readonly label: ReactNode;
  readonly help?: ReactNode;
  readonly error?: ReactNode;
  readonly required?: boolean;
  /**
   * A single child input element (or any component that forwards
   * `id`, `aria-describedby`, `aria-invalid`). Field injects the
   * accessibility wiring so consumers don't have to.
   */
  readonly children: ReactElement<{
    id?: string;
    "aria-describedby"?: string;
    "aria-invalid"?: boolean;
    "aria-required"?: boolean;
  }>;
}

export function Field({ label, help, error, required = false, children }: FieldProps): ReactElement {
  const generatedId = useId();
  const controlId = children.props.id ?? `plaidify-field-${generatedId}`;
  const helpId = help ? `${controlId}-help` : undefined;
  const errorId = error ? `${controlId}-error` : undefined;
  const describedBy =
    [children.props["aria-describedby"], helpId, errorId].filter(Boolean).join(" ") || undefined;

  const control: ReactElement = {
    ...children,
    props: {
      ...children.props,
      id: controlId,
      "aria-describedby": describedBy,
      "aria-invalid": error ? true : children.props["aria-invalid"],
      "aria-required": required || children.props["aria-required"],
    },
  } as ReactElement;

  return (
    <div className="plaidify-field">
      <label htmlFor={controlId} className="plaidify-field__label">
        {label}
        {required ? (
          <span aria-hidden="true" style={{ color: "var(--plaidify-color-danger-default)" }}>
            {" "}
            *
          </span>
        ) : null}
      </label>
      {control}
      {help ? (
        <div id={helpId} className="plaidify-field__help">
          {help}
        </div>
      ) : null}
      {error ? (
        <div id={errorId} role="alert" className="plaidify-field__error">
          {error}
        </div>
      ) : null}
    </div>
  );
}
