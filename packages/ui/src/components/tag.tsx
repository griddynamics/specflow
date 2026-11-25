import type { JSX, ReactNode } from "react";
import { cx } from "../cx";

/** A small mono label: a role, a state. */
export function Tag({ solid = false, children }: Readonly<{ solid?: boolean; children: ReactNode }>): JSX.Element {
  return <span className={cx("gx-tag", solid && "gx-tag--solid")}>{children}</span>;
}
