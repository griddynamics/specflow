// FE-DS-01, FE-DS-07 (P17 s03): the root layout owns the theme root. The theme comes from the
// `specflow-theme` cookie onto <html data-theme> (SPEC L87); missing or another value -> light, even
// when the OS prefers dark. Reading the cookie makes every page request-time rendered.
// CSS order: the app base sheet (globals.css), then the Graphico stylesheet of packages/ui. Task 4
// removed the GD design-system sheets; every `.gx` rule scopes itself to its root class, so no other
// screen changes.
import type { Metadata } from "next";
import { cookies } from "next/headers";
import { THEME_COOKIE, themeFromCookie } from "@/lib/theme";
import "./globals.css"; // 0: app base (box-sizing, body margin)
import "@graphico/ui/styles.css"; // 1: Graphico style (Mission Control V2): every rule sits under the `.gx` root class

export const metadata: Metadata = {
  title: "Graphico",
  description: "Graphico: turn a project's files into a knowledge graph, the gaps in it and a plan",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const theme = themeFromCookie((await cookies()).get(THEME_COOKIE)?.value);
  return (
    <html lang="en" data-theme={theme}>
      <body>{children}</body>
    </html>
  );
}
