// FE-DS-02 (R5): port of `Button` in bundle.js.
import type { ButtonHTMLAttributes, ReactNode, Ref } from "react";
import { cx, truthy } from "./utils";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** primary = yellow capsule (one per view); glass = secondary; plain = text with yellow underline. */
  variant?: "primary" | "glass" | "plain";
  size?: "sm" | "md" | "lg";
  /** Trailing arrow, as on the website CTAs. */
  arrow?: boolean;
  children: ReactNode;
  ref?: Ref<HTMLButtonElement>;
}

function Arrow() {
  return (
    <svg className="gdg-arrow" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M2 8h11M9 4l4 4-4 4" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Button({ variant = "primary", size = "md", arrow, className, children, type = "button", ...rest }: Readonly<ButtonProps>) {
  return (
    <button type={type} {...rest} className={cx("gdg-btn", `gdg-btn--${variant}`, size !== "md" && `gdg-btn--${size}`, className)}>
      {children}
      {truthy(arrow) ? <Arrow /> : null}
    </button>
  );
}
