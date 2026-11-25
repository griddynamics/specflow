import type { Metadata, Viewport } from "next";
import type { JSX, ReactNode } from "react";
import { GraphicoRoot } from "@graphico/ui";
import "@graphico/ui/styles.css";
import { THEME_SCRIPT } from "../lib/theme";
import "./site.css";
import "./header.css";
import "./hero.css";
import "./figures.css";
import "./how.css";
import "./haystack.css";
import "./footer.css";

export const metadata: Metadata = {
  title: "Graphico: spec-driven development at enterprise scale",
  description:
    "Graphico refines the hundreds of pages of a large project into one verified specification, finds contradictions and missing decisions, and gives coding agents work they can build from.",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, viewportFit: "cover" };

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>): JSX.Element {
  return (
    // The inline script sets data-theme on <html> before first paint, so the attribute differs from the server markup.
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="gx">
        <GraphicoRoot>{children}</GraphicoRoot>
      </body>
    </html>
  );
}
