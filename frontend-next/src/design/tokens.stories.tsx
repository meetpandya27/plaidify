import type { StoryDefault } from "@ladle/react";

import { space, radii, colors, typography, motion } from "./tokens";

export default {
  title: "Tokens",
} satisfies StoryDefault;

type Row = { key: string; value: string };

function Swatch({ name, value }: { name: string; value: string }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: 8,
        border: `1px solid ${colors.border.default}`,
        borderRadius: radii.md,
      }}
    >
      <div
        style={{
          width: 32,
          height: 32,
          borderRadius: radii.sm,
          background: value,
          border: `1px solid ${colors.border.default}`,
        }}
      />
      <div>
        <div style={{ fontWeight: 600 }}>{name}</div>
        <div style={{ color: colors.fg.muted, fontSize: typography.fontSize.xs }}>{value}</div>
      </div>
    </div>
  );
}

function Table({ rows }: { rows: Row[] }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
      {rows.map((r) => (
        <div
          key={r.key}
          style={{
            padding: 8,
            border: `1px solid ${colors.border.default}`,
            borderRadius: radii.sm,
            fontFamily: typography.fontFamily.mono,
            fontSize: typography.fontSize.xs,
            color: colors.fg.muted,
          }}
        >
          <div style={{ color: colors.fg.default, fontWeight: 600 }}>{r.key}</div>
          <div>{r.value}</div>
        </div>
      ))}
    </div>
  );
}

export const Colors = () => (
  <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 12 }}>
    <Swatch name="bg.canvas" value={colors.bg.canvas} />
    <Swatch name="bg.surface" value={colors.bg.surface} />
    <Swatch name="fg.default" value={colors.fg.default} />
    <Swatch name="fg.muted" value={colors.fg.muted} />
    <Swatch name="accent.default" value={colors.accent.default} />
    <Swatch name="success.default" value={colors.success.default} />
    <Swatch name="warning.default" value={colors.warning.default} />
    <Swatch name="danger.default" value={colors.danger.default} />
  </div>
);

export const Spacing = () => (
  <Table
    rows={Object.entries(space).map(([k, v]) => ({ key: `space.${k}`, value: v }))}
  />
);

export const Radii = () => (
  <Table rows={Object.entries(radii).map(([k, v]) => ({ key: `radii.${k}`, value: v }))} />
);

export const Motion = () => (
  <Table
    rows={[
      ...Object.entries(motion.duration).map(([k, v]) => ({ key: `duration.${k}`, value: v })),
      ...Object.entries(motion.easing).map(([k, v]) => ({ key: `easing.${k}`, value: v })),
    ]}
  />
);
