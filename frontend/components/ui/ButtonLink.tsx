// MCP-M10 (E9): a Next `Link` that looks like the design system's Button (`gdg-btn` classes, optional trailing arrow). `Button` renders a
// <button>; a control that navigates must be an anchor. Same classes, same states.
import Link from "next/link";
import type { ComponentProps } from "react";
import { Arrow } from "./Button";
import { cx, truthy } from "./utils";

export interface ButtonLinkProps extends Omit<ComponentProps<typeof Link>, "className"> {
  variant?: "primary" | "glass" | "plain";
  size?: "sm" | "md" | "lg";
  arrow?: boolean;
  className?: string;
}

export function ButtonLink({ variant = "glass", size = "md", arrow, className, children, ...rest }: Readonly<ButtonLinkProps>) {
  return (
    <Link {...rest} className={cx("gdg-btn", `gdg-btn--${variant}`, size !== "md" && `gdg-btn--${size}`, className)}>
      {children}
      {truthy(arrow) ? <Arrow /> : null}
    </Link>
  );
}
