"use client";
// FE-DS-02, FE-DOD-05.AC6 (R5): port of `SegmentedControl` in bundle.js. The lens follows the pressed segment; its
// transition is switched off by bundle.css under prefers-reduced-motion.
// Addition to bundle.js (GAPS P04): arrow keys, Home and End move the selection and the focus (roving tabindex), so
// the control is usable by keyboard like a native radio group.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { GlassSurface } from "./GlassSurface";
import { useControlled } from "./use-controlled";
import { focusedElement } from "./use-outside";
import { truthy } from "./utils";

export interface SegmentedControlProps {
  options: { value: string; label: string }[];
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  /** Accessible group name. */
  label: string;
}

export function SegmentedControl({ options, value, defaultValue, onChange, label }: Readonly<SegmentedControlProps>) {
  const [cur, set] = useControlled<string | undefined>(
    value,
    defaultValue ?? options[0]?.value,
    onChange as ((v: string | undefined) => void) | undefined,
  );
  const [buttons] = useState(() => new Map<string, HTMLButtonElement>());
  const lens = useRef<HTMLSpanElement>(null);
  const group = useRef<HTMLElement>(null);
  const tabStop = options.some((o) => o.value === cur) ? cur : options[0]?.value; // one tab stop even for an unknown value

  const place = useCallback(() => {
    const el = cur !== undefined ? buttons.get(cur) : undefined;
    if (el && lens.current) {
      lens.current.style.left = `${String(el.offsetLeft)}px`;
      lens.current.style.width = `${String(el.offsetWidth)}px`;
    }
  }, [cur, buttons]);

  useLayoutEffect(() => {
    place();
    // Fira Sans loads after the first paint and changes the segment widths: measure again when the group resizes.
    const g = group.current;
    if (!g) return;
    const ro = new ResizeObserver(place);
    ro.observe(g);
    return () => {
      ro.disconnect();
    };
  }, [place, options.length]);

  // P15: the selection changed from outside (browser Back/Forward) while a segment of this group had the focus: the focus follows
  // the pressed segment, so the ring does not stay on the old one. Clicks and arrow keys already focus the pressed segment.
  useEffect(() => {
    const active = focusedElement();
    const next = cur !== undefined ? buttons.get(cur) : undefined;
    if (next && active && active !== next && truthy(group.current?.contains(active))) next.focus();
  }, [cur, buttons]);

  function onKeyDown(e: KeyboardEvent<HTMLElement>) {
    const i = options.findIndex((o) => o.value === cur);
    let n = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") n = (i + 1) % options.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") n = (i - 1 + options.length) % options.length;
    else if (e.key === "Home") n = 0;
    else if (e.key === "End") n = options.length - 1;
    if (n < 0) return;
    e.preventDefault();
    set(options[n].value);
    buttons.get(options[n].value)?.focus();
  }

  return (
    <GlassSurface ref={group} className="gdg-seg" role="group" aria-label={label} onKeyDown={onKeyDown}>
      <span ref={lens} className="gdg-seg__lens" aria-hidden data-testid="seg-lens" />
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className="gdg-seg-btn"
          aria-pressed={cur === o.value}
          tabIndex={tabStop === o.value ? 0 : -1}
          ref={(el) => {
            if (el) buttons.set(o.value, el);
            else buttons.delete(o.value);
          }}
          onClick={() => {
            set(o.value);
          }}
        >
          {o.label}
        </button>
      ))}
    </GlassSurface>
  );
}
