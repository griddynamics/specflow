"use client";
// FE-PLAN-08.AC7, AC8, AC9 (G-PLAN-3; DECISIONS P25-3, P25-4): a code block with a Copy button. Copy uses `navigator.clipboard.writeText` and
// nothing else (R10: no network). Success: the button reads "Copied" for 2 s and a toast shows. Refusal: the text is selected and the line
// "Press Ctrl/Cmd+C" shows; no toast, no console error. The block scrolls inside its card (`.dev-code`, develop.css).
import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { TOAST_DURATION_MS, useToast } from "@/components/ui/Toast";
import { COPIED_LABEL, COPIED_MS, COPIED_TOAST, COPY_FALLBACK, COPY_LABEL } from "./labels";
import { tabStop } from "@/components/ui/utils";

type CopyState = "idle" | "copied" | "refused";

/** The status line of the copy button: the manual-copy hint after a refusal, a screen-reader note after a copy. */
function copyNote(state: CopyState) {
  if (state === "refused") return <>{COPY_FALLBACK}</>;
  return state === "copied" ? <span className="dev-sr">{COPIED_LABEL}</span> : null;
}

export function CodeBlock({ caption, text }: Readonly<{ caption: string; text: string }>) {
  const toast = useToast();
  const captionId = useId();
  const pre = useRef<HTMLPreElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const press = useRef(0); // R25-03: only the latest press may act; an unmount ends them all
  const toastedAt = useRef<number | null>(null); // P26 (P25 m1): one toast per block while its toast is still showing
  const [state, setState] = useState<CopyState>("idle");

  useEffect(
    () => () => {
      press.current = -1; // unmounted: a pending write must not touch state, toast or timers
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  async function copy() {
    const mine = ++press.current;
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    const alive = () => press.current === mine; // false after a newer press or an unmount
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      if (!alive()) return;
      setState("refused");
      const el = pre.current;
      if (el) {
        const range = document.createRange();
        range.selectNodeContents(el);
        const sel = window.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }
      return;
    }
    if (!alive()) return;
    setState("copied");
    // A toast fades after TOAST_DURATION_MS: a press inside that time shows no second one (the button already reads "Copied").
    const now = Date.now();
    if (toastedAt.current === null || now - toastedAt.current >= TOAST_DURATION_MS) {
      toastedAt.current = now;
      toast({ title: COPIED_TOAST, tone: "success" });
    }
    timer.current = setTimeout(() => {
      timer.current = null;
      if (alive()) setState("idle");
    }, COPIED_MS);
  }

  return (
    <div className="dev-block">
      <div className="dev-block__bar">
        <p className="dev-block__caption" id={captionId}>
          {caption}
        </p>
        <span className="dev-block__status" role="status">
          {copyNote(state)}
        </span>
        <Button
          variant="glass"
          size="sm"
          onClick={() => {
            void copy();
          }}
          aria-describedby={captionId}
          data-copy-state={state}
        >
          {state === "copied" ? COPIED_LABEL : COPY_LABEL}
        </Button>
      </div>
      <div className="gdg-prose dev-code">
        <pre ref={pre} {...tabStop(true)} aria-label={caption} data-testid="code-block">
          <code>{text}</code>
        </pre>
      </div>
    </div>
  );
}
