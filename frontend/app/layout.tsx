// FE-DS-01, FE-DS-04, FE-DS-07 (TECH-SPECS §6.2, §7; P17 s03): the root layout owns the design-system CSS for every route and the
// theme root. CSS order: app base sheet, tokens, components, the React Flow package CSS (TECH-SPECS §6.2). The theme comes from the
// `specflow-theme` cookie onto <html data-theme> (SPEC L87); missing or another value -> light, even when the OS prefers dark.
// Reading the cookie makes every page request-time rendered.
import type { Metadata } from "next";
import { cookies } from "next/headers";
import { THEME_COOKIE, themeFromCookie } from "@/lib/theme";
import "./globals.css"; // 0: app base (body, theme-root frame); tokens only
import "@/styles/ds/tokens.css"; // 1: tokens first
import "@/styles/ds/components/bundle.css"; // 2
import "@/styles/ds-a11y.css"; // 2b: reduced-motion and focus cover of the frozen bundle copy (WCAG 2.2 SC 2.3.3, 2.4.7)

export const metadata: Metadata = {
  title: "Specflow",
  description: "Specflow: turn a project's files into a knowledge graph, decisions and answers",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const theme = themeFromCookie((await cookies()).get(THEME_COOKIE)?.value);
  return (
    <html lang="en" data-theme={theme}>
      <body>{children}</body>
    </html>
  );
}
