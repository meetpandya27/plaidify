import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

export type ToastTone = "neutral" | "success" | "warning" | "danger";

export interface ToastInput {
  readonly id?: string;
  readonly message: ReactNode;
  readonly tone?: ToastTone;
  readonly durationMs?: number;
}

interface Toast extends ToastInput {
  readonly id: string;
  readonly tone: ToastTone;
}

interface ToastContextValue {
  readonly toasts: ReadonlyArray<Toast>;
  readonly push: (input: ToastInput) => string;
  readonly dismiss: (id: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export interface ToastProviderProps {
  readonly children: ReactNode;
  readonly defaultDurationMs?: number;
}

export function ToastProvider({
  children,
  defaultDurationMs = 5000,
}: ToastProviderProps) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: string) => {
    const handle = timers.current.get(id);
    if (handle) {
      clearTimeout(handle);
      timers.current.delete(id);
    }
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (input: ToastInput): string => {
      const id =
        input.id ??
        (typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `toast-${Math.random().toString(36).slice(2)}`);
      const toast: Toast = {
        id,
        message: input.message,
        tone: input.tone ?? "neutral",
        durationMs: input.durationMs ?? defaultDurationMs,
      };
      setToasts((prev) => [...prev, toast]);
      if (toast.durationMs && toast.durationMs > 0) {
        const handle = setTimeout(() => dismiss(id), toast.durationMs);
        timers.current.set(id, handle);
      }
      return id;
    },
    [defaultDurationMs, dismiss],
  );

  const value = useMemo<ToastContextValue>(
    () => ({ toasts, push, dismiss }),
    [toasts, push, dismiss],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastRegion />
    </ToastContext.Provider>
  );
}

function ToastRegion() {
  const ctx = useContext(ToastContext);
  if (!ctx) return null;
  return (
    <div className="plaidify-toast-region" aria-live="polite" aria-atomic="false">
      {ctx.toasts.map((t) => (
        <div
          key={t.id}
          role={t.tone === "danger" ? "alert" : "status"}
          className={`plaidify-toast plaidify-toast--${t.tone}`}
        >
          {t.message}
        </div>
      ))}
    </div>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used inside a ToastProvider.");
  }
  return ctx;
}
