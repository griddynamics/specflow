import type { JSX, ReactNode } from "react";
import { cx } from "../cx";

export interface CardProps {
  /** The element to render; a list item or a section where the page structure needs one. */
  as?: "div" | "section" | "article" | "li";
  className?: string;
  children?: ReactNode;
}

/** A bordered panel on the paper colour. */
export function Card({ as: Element = "div", className, children }: Readonly<CardProps>): JSX.Element {
  return <Element className={cx("gx-card", className)}>{children}</Element>;
}
