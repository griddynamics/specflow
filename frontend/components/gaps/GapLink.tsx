"use client";
// FR-GWEB-04: a link to another gap's question card inside the Gaps screen. It is a real link (`gapLink`, so it opens in a new tab and can be
// copied); a plain click opens the card in place (the screen swaps `?gap=` without a navigation).
import type { MouseEvent, ReactNode } from "react";

export function GapLink({
  href,
  onOpen,
  testId,
  children,
}: Readonly<{ href: string; onOpen: () => void; testId?: string; children: ReactNode }>) {
  const click = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // a new tab or window: the browser follows the link
    e.preventDefault();
    onOpen();
  };
  return (
    <a className="gap-link gdg-focus" href={href} onClick={click} data-testid={testId}>
      {children}
    </a>
  );
}
