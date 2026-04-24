export interface ProgressDotsProps {
  readonly label?: string;
}

/**
 * Three-dot animated indeterminate progress indicator. Uses CSS
 * keyframes driven by motion tokens so animations respect
 * prefers-reduced-motion automatically.
 */
export function ProgressDots({ label = "Loading" }: ProgressDotsProps) {
  return (
    <span
      className="plaidify-progress-dots"
      role="status"
      aria-live="polite"
      aria-label={label}
    >
      <span className="plaidify-progress-dots__dot" />
      <span className="plaidify-progress-dots__dot" />
      <span className="plaidify-progress-dots__dot" />
    </span>
  );
}
