// FE-DS-02 (R5): port of `Card` in bundle.js.
import type { CSSProperties, ReactNode } from "react";
import { GlassSurface } from "./GlassSurface";
import { cx, truthy } from "./utils";

export interface CardProps {
  variant?: "regular" | "strong";
  /** UPPERCASE kicker. */
  eyebrow?: string;
  title?: string;
  children?: ReactNode;
  /** Actions or tags row. */
  footer?: ReactNode;
  className?: string;
  style?: CSSProperties;
}

export function Card({ variant = "regular", eyebrow, title, children, footer, className, style }: Readonly<CardProps>) {
  return (
    <GlassSurface variant={variant} className={cx("gdg-card", className)} style={style} data-testid="card">
      {truthy(eyebrow) ? (
        <p className="gdg-card__eyebrow" data-testid="card-eyebrow">
          {eyebrow}
        </p>
      ) : null}
      {truthy(title) ? <h3 className="gdg-card__title">{title}</h3> : null}
      {truthy(children) ? <div className="gdg-card__body">{children}</div> : null}
      {truthy(footer) ? <div className="gdg-card__footer">{footer}</div> : null}
    </GlassSurface>
  );
}
