// FE-DS-02 (R5): port of `Tag` in bundle.js. `warning` and `pending` are in bundle.js and its preview but missing from index.d.ts (GAPS P04).
import type { ReactNode } from "react";
import { cx } from "./utils";

export interface TagProps {
  tone?: "neutral" | "accent" | "info" | "success" | "danger" | "warning" | "pending";
  className?: string;
  children: ReactNode;
}

export function Tag({ tone = "neutral", className, children }: Readonly<TagProps>) {
  const status = tone === "info" || tone === "success" || tone === "danger" || tone === "warning";
  return (
    <span className={cx("gdg-tag", tone !== "neutral" && `gdg-tag--${tone}`, className)} data-testid="tag">
      {status ? <span className="gdg-tag__dot" aria-hidden /> : null}
      {!status && tone === "pending" ? <span className="gdg-tag__spin" aria-hidden data-testid="tag-spin" /> : null}
      {children}
    </span>
  );
}
