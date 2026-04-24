/**
 * Skeleton loader primitives for the hosted-link flow (issue #60).
 *
 * These are aria-hidden placeholders rendered while a network-bound
 * state (directory search, MFA poll) is in flight. Real content
 * replaces them as soon as the first response arrives — the live
 * region announces step changes so screen-reader users aren't left
 * parsing decorative skeletons.
 *
 * All shimmer animation is driven by CSS so the shared
 * `prefers-reduced-motion` rule in link.css disables it automatically.
 */
import type { ReactElement } from "react";

export interface SkeletonRowListProps {
  readonly rows?: number;
}

export function SkeletonRowList({ rows = 4 }: SkeletonRowListProps): ReactElement {
  return (
    <ul className="skeleton-list" aria-hidden="true" data-testid="skeleton-row-list">
      {Array.from({ length: rows }).map((_, i) => (
        <li key={i} className="skeleton skeleton--row" />
      ))}
    </ul>
  );
}

export function SkeletonText({
  widthPct = 100,
}: {
  readonly widthPct?: number;
}): ReactElement {
  return (
    <span
      className="skeleton skeleton--text"
      aria-hidden="true"
      style={{ width: `${widthPct}%` }}
    />
  );
}
