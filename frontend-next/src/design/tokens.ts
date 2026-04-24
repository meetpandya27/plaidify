/**
 * Typed mirror of the CSS tokens defined in tokens.css. Every value
 * here is a CSS variable expression (e.g. `"var(--plaidify-space-4)"`)
 * so consumers stay bound to the theme — flipping `data-theme="dark"`
 * on the document root changes the rendered value, not the reference.
 *
 * Durations are additionally exported in a `ms` map because a handful
 * of components (e.g. EventDelivery retry decisions, focus-trap polls)
 * need the numeric value.
 */

function cssVar(name: string): string {
  return `var(--plaidify-${name})`;
}

export const colors = {
  bg: {
    canvas: cssVar("color-bg-canvas"),
    surface: cssVar("color-bg-surface"),
    surfaceAlt: cssVar("color-bg-surface-alt"),
    overlay: cssVar("color-bg-overlay"),
  },
  fg: {
    default: cssVar("color-fg-default"),
    muted: cssVar("color-fg-muted"),
    subtle: cssVar("color-fg-subtle"),
    onAccent: cssVar("color-fg-on-accent"),
  },
  border: {
    default: cssVar("color-border-default"),
    strong: cssVar("color-border-strong"),
    focus: cssVar("color-border-focus"),
  },
  accent: {
    default: cssVar("color-accent-default"),
    hover: cssVar("color-accent-hover"),
    active: cssVar("color-accent-active"),
    subtle: cssVar("color-accent-subtle"),
  },
  success: {
    default: cssVar("color-success-default"),
    subtle: cssVar("color-success-subtle"),
  },
  warning: {
    default: cssVar("color-warning-default"),
    subtle: cssVar("color-warning-subtle"),
  },
  danger: {
    default: cssVar("color-danger-default"),
    subtle: cssVar("color-danger-subtle"),
  },
} as const;

export const space = {
  0: cssVar("space-0"),
  1: cssVar("space-1"),
  2: cssVar("space-2"),
  3: cssVar("space-3"),
  4: cssVar("space-4"),
  5: cssVar("space-5"),
  6: cssVar("space-6"),
  8: cssVar("space-8"),
  10: cssVar("space-10"),
  12: cssVar("space-12"),
  16: cssVar("space-16"),
} as const;

export const radii = {
  xs: cssVar("radius-xs"),
  sm: cssVar("radius-sm"),
  md: cssVar("radius-md"),
  lg: cssVar("radius-lg"),
  xl: cssVar("radius-xl"),
  pill: cssVar("radius-pill"),
} as const;

export const shadows = {
  xs: cssVar("shadow-xs"),
  sm: cssVar("shadow-sm"),
  md: cssVar("shadow-md"),
  lg: cssVar("shadow-lg"),
  focus: cssVar("shadow-focus"),
} as const;

export const typography = {
  fontFamily: {
    sans: cssVar("font-family-sans"),
    mono: cssVar("font-family-mono"),
  },
  fontSize: {
    xs: cssVar("font-size-xs"),
    sm: cssVar("font-size-sm"),
    md: cssVar("font-size-md"),
    lg: cssVar("font-size-lg"),
    xl: cssVar("font-size-xl"),
    "2xl": cssVar("font-size-2xl"),
    "3xl": cssVar("font-size-3xl"),
  },
  lineHeight: {
    tight: cssVar("line-height-tight"),
    normal: cssVar("line-height-normal"),
    relaxed: cssVar("line-height-relaxed"),
  },
  fontWeight: {
    regular: cssVar("font-weight-regular"),
    medium: cssVar("font-weight-medium"),
    semibold: cssVar("font-weight-semibold"),
    bold: cssVar("font-weight-bold"),
  },
} as const;

export const motion = {
  duration: {
    instant: cssVar("duration-instant"),
    fast: cssVar("duration-fast"),
    base: cssVar("duration-base"),
    slow: cssVar("duration-slow"),
  },
  easing: {
    standard: cssVar("easing-standard"),
    emphasized: cssVar("easing-emphasized"),
    exit: cssVar("easing-exit"),
    springSoft: cssVar("spring-soft"),
    springBouncy: cssVar("spring-bouncy"),
  },
  /** Numeric durations in milliseconds for JS timers. */
  durationMs: {
    instant: 80,
    fast: 140,
    base: 220,
    slow: 360,
  },
} as const;

export const layout = {
  maxWidthContent: cssVar("max-width-content"),
  zIndexDialog: cssVar("z-index-dialog"),
  zIndexToast: cssVar("z-index-toast"),
} as const;

export const tokens = {
  colors,
  space,
  radii,
  shadows,
  typography,
  motion,
  layout,
} as const;

export type Tokens = typeof tokens;
