// FE-DS-02 (R5): port of `GlassSurface` in bundle.js, restyled in the Graphico way (Task 4): flat
// ink-on-paper surfaces and a hard offset shadow instead of Liquid Glass. Props, the element
// contract and the variant meanings are unchanged.
import { createElement, type CSSProperties, type ElementType, type HTMLAttributes, type ReactNode, type Ref } from "react";
import { cx, truthy } from "./utils";
import "./shared-ui.css";

export interface GlassSurfaceProps extends Omit<HTMLAttributes<HTMLElement>, "className" | "style" | "children"> {
  /** regular = wash panel (ink text only); strong = solid paper card (any text); subtle = clear panel; tint = the yellow accent. */
  variant?: "regular" | "strong" | "subtle" | "tint";
  /** Element to render. Default 'div'. */
  as?: keyof React.JSX.IntrinsicElements;
  /** Adds the fine vertical rules of the brand ribbing. Large hero surfaces only. */
  ribbed?: boolean;
  /** Uses the hard offset shadow for layers above other surfaces. */
  floating?: boolean;
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
  ref?: Ref<HTMLElement>;
}

// The variant classes spelled out, not `sui-glass--${variant}`: every class name is a literal (the
// evidence the css-usage check needs) and a typo falls out of the type.
const VARIANT_CLASS: Record<NonNullable<GlassSurfaceProps["variant"]>, string> = {
  regular: "",
  strong: "sui-glass--strong",
  subtle: "sui-glass--subtle",
  tint: "sui-glass--tint",
};

export function GlassSurface({ variant = "regular", as = "div", ribbed, floating, className, children, ...rest }: GlassSurfaceProps) {
  return createElement(
    as as ElementType,
    {
      ...rest,
      className: cx(
        "sui-glass",
        VARIANT_CLASS[variant],
        truthy(ribbed) && "sui-glass--ribbed",
        truthy(floating) && "sui-glass--float",
        className,
      ),
    },
    children,
  );
}
