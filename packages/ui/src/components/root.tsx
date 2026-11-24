import type { JSX, ReactNode } from "react";
import { cx } from "../cx";

/** Fira Sans and Fira Code from Google Fonts: no font files are bundled, and fonts.googleapis.com and fonts.gstatic.com are the only font hosts. */
export const FONT_STYLESHEET =
  "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;600&family=Fira+Sans:wght@300;400;600;700;800&display=swap";

export type GraphicoTheme = "light" | "dark";

export interface GraphicoRootProps {
  /** Force a theme. Without it the OS preference decides. */
  theme?: GraphicoTheme;
  /** Load the fonts from Google Fonts. Turn it off where the host page already does. */
  fonts?: boolean;
  className?: string;
  children?: ReactNode;
}

/** The root of every Graphico screen: carries the `gx` class (tokens, base styles) and the font links. Import "@graphico/ui/styles.css" once. */
export function GraphicoRoot({ theme, fonts = true, className, children }: Readonly<GraphicoRootProps>): JSX.Element {
  return (
    <div className={cx("gx", className)} data-theme={theme}>
      {fonts ? (
        <>
          <link rel="preconnect" href="https://fonts.googleapis.com" />
          <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
          <link rel="stylesheet" href={FONT_STYLESHEET} precedence="graphico-fonts" />
        </>
      ) : null}
      {children}
    </div>
  );
}
