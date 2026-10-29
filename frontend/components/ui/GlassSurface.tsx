// FE-DS-02 (R5): port of `GlassSurface` in bundle.js. The Liquid Glass material every other component builds on.
import { createElement, type CSSProperties, type ElementType, type HTMLAttributes, type ReactNode, type Ref } from "react";
import { cx, truthy } from "./utils";

export interface GlassSurfaceProps extends Omit<HTMLAttributes<HTMLElement>, "className" | "style" | "children"> {
  /** regular = glass-fill (ink text only); strong = glass-fill-strong (any text); subtle = clear inset; tint = yellow lens. */
  variant?: "regular" | "strong" | "subtle" | "tint";
  /** Element to render. Default 'div'. */
  as?: keyof React.JSX.IntrinsicElements;
  /** Adds the fine vertical ribbing from the brand artwork. Large hero glass only. */
  ribbed?: boolean;
  /** Uses shadow-glass-float for layers above other glass. */
  floating?: boolean;
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
  ref?: Ref<HTMLElement>;
}

export function GlassSurface({ variant = "regular", as = "div", ribbed, floating, className, children, ...rest }: GlassSurfaceProps) {
  return createElement(
    as as ElementType,
    {
      ...rest,
      className: cx(
        "gdg-glass",
        variant !== "regular" && `gdg-glass--${variant}`,
        truthy(ribbed) && "gdg-glass--ribbed",
        truthy(floating) && "gdg-glass--float",
        className,
      ),
    },
    children,
  );
}
