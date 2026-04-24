import { useCallback, useEffect, useRef } from "react";
import type { KeyboardEvent, ReactNode } from "react";

export interface DialogProps {
  readonly open: boolean;
  readonly onClose?: () => void;
  readonly title: ReactNode;
  readonly description?: ReactNode;
  readonly children?: ReactNode;
  readonly labelledBy?: string;
  readonly describedBy?: string;
}

/**
 * Minimal accessible dialog. Not a full Radix replacement — it focuses
 * the first focusable child on open, restores focus on close, and
 * forwards Escape to `onClose`. The hosted-link flow only uses dialogs
 * for confirmation/error copy, so this surface is intentionally small.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  labelledBy,
  describedBy,
}: DialogProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const returnFocusTo = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    returnFocusTo.current = (typeof document !== "undefined"
      ? (document.activeElement as HTMLElement | null)
      : null);
    const root = dialogRef.current;
    if (root) {
      const focusable = root.querySelector<HTMLElement>(
        'a, button, [tabindex]:not([tabindex="-1"]), input, select, textarea',
      );
      (focusable ?? root).focus();
    }
    return () => {
      if (returnFocusTo.current) {
        try {
          returnFocusTo.current.focus();
        } catch {
          // Element may be unmounted — ignore.
        }
      }
    };
  }, [open]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape" && onClose) {
        event.stopPropagation();
        onClose();
      }
    },
    [onClose],
  );

  if (!open) {
    return null;
  }

  return (
    <div
      className="plaidify-dialog__backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget && onClose) {
          onClose();
        }
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy ?? "plaidify-dialog-title"}
        aria-describedby={describedBy ?? (description ? "plaidify-dialog-description" : undefined)}
        tabIndex={-1}
        className="plaidify-dialog"
        onKeyDown={handleKeyDown}
      >
        <h2 id={labelledBy ?? "plaidify-dialog-title"} className="plaidify-dialog__title">
          {title}
        </h2>
        {description ? (
          <p
            id={describedBy ?? "plaidify-dialog-description"}
            className="plaidify-dialog__description"
          >
            {description}
          </p>
        ) : null}
        {children}
      </div>
    </div>
  );
}
