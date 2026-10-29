"use client";
// FE-DOD-05.AC5 (R5): `useOutside` from bundle.js part 3: while `open`, a mousedown outside `ref` or Esc calls `close`.
// This module is the one owner of dismissal: the outside click and Esc of a popup (`useOutside`), Esc, Tab confinement and focus return
// of a dialog (`useModalDismissal`), and the reads of where the focus is (`focusedElement`, `focusIsLost`).
import { useEffect, useRef, type RefObject } from "react";

/** Why a popup is asked to close. */
export type CloseReason = "outside" | "escape";

export function useOutside(ref: RefObject<HTMLElement | null>, open: boolean, close: (reason: CloseReason) => void): void {
  const closeRef = useRef(close);
  useEffect(() => {
    closeRef.current = close;
  });
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      // composedPath keeps the original target even when React has already removed it from the DOM (a picked option)
      if (ref.current && !e.composedPath().includes(ref.current)) closeRef.current("outside");
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current("escape");
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, ref]);
}

/** The element that has the focus now (null when nothing has it). */
export function focusedElement(): Element | null {
  return document.activeElement;
}

/** True when the focus is on `<body>` or nowhere, e.g. after the control that had it disappeared. */
export function focusIsLost(): boolean {
  const active = document.activeElement;
  return active === null || active === document.body;
}

// The open dialogs, oldest first. A preview (`noAutoFocus`) is not modal and never joins.
const OPEN: symbol[] = [];

// Real tab stops only: a native control or [tabindex] that is not tabindex=-1 and not disabled.
const FOCUSABLE =
  'button:not(:disabled):not([tabindex="-1"]),[href]:not([tabindex="-1"]),input:not(:disabled):not([tabindex="-1"]),textarea:not(:disabled):not([tabindex="-1"]),select:not(:disabled):not([tabindex="-1"]),[tabindex]:not([tabindex="-1"]):not(:disabled)';

/** Tab inside a dialog: confines the focus to its controls (wraps first <-> last, pulls it back in from outside). */
function confineTab(e: KeyboardEvent, el: HTMLElement): void {
  const els = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE));
  if (!els.length) {
    e.preventDefault();
    el.focus();
    return;
  }
  const first = els[0];
  const last = els[els.length - 1];
  const active = document.activeElement;
  if (!el.contains(active)) {
    e.preventDefault();
    (e.shiftKey ? last : first).focus();
  } else if (e.shiftKey && (active === first || active === el)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

export interface ModalDismissal {
  /** The dialog element. */
  ref: RefObject<HTMLElement | null>;
  open: boolean;
  /** An embedded preview: the focus stays where it is, nothing is trapped, Esc does nothing. */
  noAutoFocus: boolean;
  /** Where the focus lands on open: the first field or button, or the dialog itself. Read once, on open. */
  initialFocus: "first" | "dialog";
  /** Called on Esc by the top dialog. */
  onEscape: () => void;
}

/**
 * On open the focus moves into the dialog and Tab stays inside it; Esc calls `onEscape`; on close the focus returns to the opener. Dialogs
 * open together keep a stack: only the top one handles Esc and Tab, so one Esc closes one dialog.
 */
export function useModalDismissal({ ref, open, noAutoFocus, initialFocus, onEscape }: Readonly<ModalDismissal>): void {
  const escapeRef = useRef(onEscape);
  const initialFocusRef = useRef(initialFocus);
  useEffect(() => {
    escapeRef.current = onEscape;
    initialFocusRef.current = initialFocus;
  });
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const me = Symbol("modal");
    if (!noAutoFocus) OPEN.push(me);
    if (el && !noAutoFocus) {
      const first =
        initialFocusRef.current === "dialog" ? null : el.querySelector<HTMLElement>("input,textarea,select,button:not(.gdg-modal__close)");
      (first ?? el).focus();
    }
    function onKey(e: KeyboardEvent) {
      if (noAutoFocus) return; // an embedded preview dialog neither traps Tab nor listens for Esc
      if (OPEN[OPEN.length - 1] !== me) return; // a dialog opened on top of this one owns Esc and Tab
      if (e.key === "Escape") escapeRef.current();
      if (e.key === "Tab" && el) confineTab(e, el);
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const at = OPEN.indexOf(me);
      if (at >= 0) OPEN.splice(at, 1);
      if (prev && !noAutoFocus) prev.focus();
    };
  }, [open, noAutoFocus, ref]);
}
