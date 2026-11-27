"use client";
// FE-DS-02, FE-DS-06 (R5): port of `Icon` in bundle.js. Phosphor Regular glyph, 256 viewBox, `currentColor`.
import { ICON_MAP, type IconName } from "./icon-map";
import { cx, truthy } from "./utils";

export type { IconName } from "./icon-map";

export interface IconProps {
  name: IconName;
  /** 16, 20 (default) or 24. */
  size?: number;
  /** Accessible name when the icon stands alone; omit for decorative icons. */
  label?: string;
  className?: string;
}

// Looked up by any string: a name outside the set falls back to `cube`, like bundle.js.
const GLYPHS: Partial<Record<string, (typeof ICON_MAP)[IconName]>> = ICON_MAP;

export function Icon({ name, size = 20, label, className }: Readonly<IconProps>) {
  const Glyph = GLYPHS[name] ?? ICON_MAP.cube;
  return (
    <Glyph
      className={cx("gdg-icon", className)}
      size={size}
      aria-hidden={truthy(label) ? undefined : true}
      role={truthy(label) ? "img" : undefined}
      aria-label={label}
    />
  );
}
