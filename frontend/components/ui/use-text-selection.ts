"use client";
// FE-FILES-03, FE-REV-01 (R5): `useTextSelection(containerRef)` from bundle.js part 3 -> [selection | null, clear].
// `x`, `y` and `bottom` are relative to the container (plus its scroll), so a SelectionToolbar placed with
// position:absolute inside the same positioned container lands above the selection. Updates on mouseup and keyup
// (after a tick, when the browser has settled the selection). It never clears the selection itself; only `clear` does.
import { useEffect, useState, type RefObject } from "react";
import { selectionAnchor } from "./app-logic";

export interface TextSelection {
  text: string;
  range: Range;
  /** Horizontal centre of the selection, relative to the container. */
  x: number;
  /** Top of the selection, relative to the container. */
  y: number;
  /** Bottom of the selection, relative to the container. */
  bottom: number;
}

/** The pause after a mouse or key release, so the browser has settled the selection before it is read. */
const SELECTION_SETTLE_MS = 0;

export function useTextSelection(containerRef: RefObject<HTMLElement | null>): [TextSelection | null, () => void] {
  const [sel, setSel] = useState<TextSelection | null>(null);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    function update() {
      const c = containerRef.current;
      const ws = window.getSelection();
      if (!c || !ws || ws.rangeCount === 0 || ws.isCollapsed) {
        setSel(null);
        return;
      }
      const r = ws.getRangeAt(0);
      if (!c.contains(r.commonAncestorContainer)) {
        setSel(null);
        return;
      }
      const rr = r.getBoundingClientRect();
      const cr = c.getBoundingClientRect();
      setSel({ text: ws.toString(), range: r.cloneRange(), ...selectionAnchor(rr, cr, { left: c.scrollLeft, top: c.scrollTop }) });
    }
    const onUp = () => {
      clearTimeout(timer);
      timer = setTimeout(update, SELECTION_SETTLE_MS);
    };
    document.addEventListener("mouseup", onUp);
    document.addEventListener("keyup", onUp);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("mouseup", onUp);
      document.removeEventListener("keyup", onUp);
    };
  }, [containerRef]);
  return [
    sel,
    () => {
      setSel(null);
      window.getSelection()?.removeAllRanges();
    },
  ];
}
