import { siConfluence, siGithub, siGoogledrive, siJira, siLinear, siModelcontextprotocol } from "simple-icons";
import type { JSX } from "react";
import { cx } from "../cx";

/** One-colour vendor marks: the `simple-icons` path data, unchanged. */
export const LOGO_PATHS = {
  github: siGithub.path,
  jira: siJira.path,
  linear: siLinear.path,
  confluence: siConfluence.path,
  gdrive: siGoogledrive.path,
  mcp: siModelcontextprotocol.path,
} as const;

/** Signs the prototype draws itself (stroke, 24 x 24): Slack is not in simple-icons, so it is a channel sign. */
export const GLYPH_PATHS = {
  slack: "M9.5 3.5 7.5 20.5M16.5 3.5l-2 17M4.5 9h16M3.5 15h16",
  upload: "M12 15V4M7.5 8.5 12 4l4.5 4.5M4 14v5h16v-5",
} as const;

export type LogoKind = keyof typeof LOGO_PATHS;
export type GlyphKind = keyof typeof GLYPH_PATHS;
export type VendorKind = LogoKind | GlyphKind;

export interface VendorMarkProps {
  kind: VendorKind;
  /** Accessible name. Without it the mark is decorative (hidden from screen readers). */
  title?: string;
  className?: string;
}

function isLogo(kind: VendorKind): kind is LogoKind {
  return kind in LOGO_PATHS;
}

/** A vendor mark in the text colour: GitHub, Jira, Linear, Confluence, Google Drive and MCP from simple-icons; Slack and upload as drawn signs. */
export function VendorMark({ kind, title, className }: VendorMarkProps): JSX.Element {
  const a11y = title === undefined ? { "aria-hidden": true as const } : { role: "img" as const, "aria-label": title };
  return isLogo(kind) ? (
    <svg {...a11y} className={cx("gx-mark gx-mark--logo", className)} viewBox="0 0 24 24" focusable="false">
      <path d={LOGO_PATHS[kind]} />
    </svg>
  ) : (
    <svg {...a11y} className={cx("gx-mark gx-mark--glyph", className)} viewBox="0 0 24 24" focusable="false">
      <path d={GLYPH_PATHS[kind]} />
    </svg>
  );
}
