import type { JSX } from "react";
import { cx } from "../cx";

/** The Graphico mark: a rounded square in the text colour with the Graph, Gap, Plan nodes cut out of it. */
export function BrandMark({ className }: Readonly<{ className?: string }>): JSX.Element {
  return (
    <svg className={cx("gx-brand__mark", className)} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="1" y="1" width="22" height="22" rx="5" fill="currentColor" />
      <path d="M7 7.5 L17 12 L7 16.5" fill="none" stroke="var(--gx-paper)" strokeWidth="1.6" />
      <circle cx="7" cy="7.5" r="2.1" fill="var(--gx-paper)" />
      <circle cx="17" cy="12" r="2.1" fill="var(--gx-paper)" />
      <circle cx="7" cy="16.5" r="2.1" fill="var(--gx-paper)" />
    </svg>
  );
}

export interface BrandProps {
  /** Makes the brand a link (the home page). */
  href?: string;
  /** Accessible name of the whole brand; the visible word is hidden on a narrow header when `compact` is set. */
  name?: string;
  /** Hide the word and keep the mark; the name stays available to screen readers. */
  compact?: boolean;
}

/** Mark plus word. */
export function Brand({ href, name = "Graphico", compact = false }: Readonly<BrandProps>): JSX.Element {
  const content = (
    <>
      <BrandMark />
      <span className={compact ? "gx-vh" : undefined}>{name}</span>
    </>
  );
  return href === undefined ? (
    <span className="gx-brand">{content}</span>
  ) : (
    <a className="gx-brand" href={href}>
      {content}
    </a>
  );
}
