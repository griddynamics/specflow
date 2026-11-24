import type { ComponentPropsWithoutRef, JSX } from "react";
import { cx } from "../cx";

export type ButtonVariant = "ink" | "ghost" | "yellow";
export type ButtonSize = "md" | "sm";

interface LookProps {
  /** `ink` is the default; `ghost` is the quiet action; `yellow` is the one action a person is needed for (once per view). */
  variant?: ButtonVariant;
  size?: ButtonSize;
}

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  ink: "",
  ghost: "gx-btn--ghost",
  yellow: "gx-btn--yellow",
};

const SIZE_CLASS: Record<ButtonSize, string> = { md: "", sm: "gx-btn--sm" };

function buttonClass(variant: ButtonVariant, size: ButtonSize, className: string | undefined): string {
  return cx("gx-btn", VARIANT_CLASS[variant], SIZE_CLASS[size], className);
}

export type ButtonProps = ComponentPropsWithoutRef<"button"> & LookProps;

/** A pill button. `type` defaults to "button" so it never submits a form by accident. */
export function Button({ variant = "ink", size = "md", className, type = "button", ...rest }: ButtonProps): JSX.Element {
  return <button {...rest} type={type} className={buttonClass(variant, size, className)} />;
}

export type ButtonLinkProps = ComponentPropsWithoutRef<"a"> & LookProps;

/** A link that looks like a button. */
export function ButtonLink({ variant = "ink", size = "md", className, ...rest }: ButtonLinkProps): JSX.Element {
  return <a {...rest} className={buttonClass(variant, size, className)} />;
}
