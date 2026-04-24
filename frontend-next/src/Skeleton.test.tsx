import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";

import { SkeletonRowList, SkeletonText } from "./Skeleton";

describe("Skeleton primitives", () => {
  it("SkeletonRowList renders the requested number of decorative rows", () => {
    const html = renderToString(<SkeletonRowList rows={5} />);
    const matches = html.match(/skeleton skeleton--row/g) ?? [];
    expect(matches).toHaveLength(5);
    expect(html).toContain('aria-hidden="true"');
    expect(html).toContain('data-testid="skeleton-row-list"');
  });

  it("SkeletonRowList defaults to 4 rows", () => {
    const html = renderToString(<SkeletonRowList />);
    const matches = html.match(/skeleton skeleton--row/g) ?? [];
    expect(matches).toHaveLength(4);
  });

  it("SkeletonText renders with the requested width", () => {
    const html = renderToString(<SkeletonText widthPct={42} />);
    expect(html).toContain("width:42%");
    expect(html).toContain('aria-hidden="true"');
  });
});
