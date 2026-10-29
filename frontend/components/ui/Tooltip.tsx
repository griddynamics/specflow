// FE-DS-02 (R5): port of `Tooltip` in bundle.js. Pure CSS (`data-tip`): shows on hover and on keyboard focus inside.
import type { ReactNode } from "react";
import { cx } from "./utils";

export interface TooltipProps {
  label: string;
  side?: "top" | "right" | "bottom" | "left";
  children: ReactNode;
}

export function Tooltip({ label, side = "top", children }: Readonly<TooltipProps>) {
  return (
    <span className={cx("gdg-tip", `gdg-tip--${side}`)} data-tip={label} data-testid="tooltip">
      {children}
    </span>
  );
}
