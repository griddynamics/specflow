"use client";
// FR-GWEB-04, FR-GRTE-06: keeps the reader's place in the Gaps inbox while rows arrive or leave above it (a live re-read during a gap
// run, a write, the check line growing). A geometry module: it measures where the topmost row in view is and scrolls by as much as that
// row moved, which CSS cannot state; the browser's own scroll anchoring stops at the list's box and leaves the rows inside it moving,
// so the screen opts out of it (gaps.css `overflow-anchor`).
import { useEffect, useLayoutEffect, useRef } from "react";

/** The topmost row fully in view once the list's first row has scrolled out of view, and where it is; null otherwise. */
function placeOf(body: HTMLElement | null): { row: HTMLElement; top: number } | null {
  const rows = body === null ? [] : [...body.querySelectorAll<HTMLElement>(".gdg-dlist__rows > li")];
  if (rows.length === 0 || rows[0].getBoundingClientRect().top >= 0) return null;
  const row = rows.find((r) => r.getBoundingClientRect().top >= 0);
  return row === undefined ? null : { row, top: row.getBoundingClientRect().top };
}

/** After every render of the screen, scrolls by as much as the topmost row in view moved; the ref goes on the tab's content. */
export function useKeepPlace() {
  const body = useRef<HTMLDivElement>(null);
  const place = useRef<{ row: HTMLElement; top: number } | null>(null);
  useLayoutEffect(() => {
    const was = place.current;
    if (was?.row.isConnected === true) {
      const moved = was.row.getBoundingClientRect().top - was.top;
      if (moved !== 0) window.scrollBy(0, moved);
    }
    place.current = placeOf(body.current);
  });
  useEffect(() => {
    const capture = () => {
      place.current = placeOf(body.current);
    };
    window.addEventListener("scroll", capture, { passive: true });
    window.addEventListener("resize", capture);
    return () => {
      window.removeEventListener("scroll", capture);
      window.removeEventListener("resize", capture);
    };
  }, []);
  return body;
}
