/**
 * Single entry point for the design system. Importing this module also
 * registers the CSS tokens and primitive styles — any app that uses a
 * primitive gets the full token layer for free, which keeps theming
 * consistent.
 */

import "./tokens.css";
import "./primitives.css";

export { tokens, colors, space, radii, shadows, typography, motion, layout } from "./tokens";
export type { Tokens } from "./tokens";
export { Button } from "./Button";
export type { ButtonProps, ButtonVariant } from "./Button";
export { Input } from "./Input";
export type { InputProps } from "./Input";
export { Field } from "./Field";
export type { FieldProps } from "./Field";
export { Dialog } from "./Dialog";
export type { DialogProps } from "./Dialog";
export { ProgressDots } from "./ProgressDots";
export type { ProgressDotsProps } from "./ProgressDots";
export { ToastProvider, useToast } from "./Toast";
export type { ToastInput, ToastTone, ToastProviderProps } from "./Toast";
