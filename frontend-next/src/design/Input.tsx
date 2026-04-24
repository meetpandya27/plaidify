import { forwardRef } from "react";
import type { InputHTMLAttributes } from "react";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  readonly invalid?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { invalid = false, className, ...rest },
  ref,
) {
  const classes = ["plaidify-input"];
  if (invalid) {
    classes.push("plaidify-input--invalid");
  }
  if (className) {
    classes.push(className);
  }
  return (
    <input
      ref={ref}
      className={classes.join(" ")}
      aria-invalid={invalid || undefined}
      {...rest}
    />
  );
});
