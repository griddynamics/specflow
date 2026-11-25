"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { JSX, ReactNode } from "react";
import { Button } from "./button";
import { createCopier } from "./copy-state";
import { VendorMark } from "./vendor-mark";
import type { VendorKind } from "./vendor-mark";

/** A drop target for files with a button that opens the picker. The files go to `onFiles`; nothing is read here. */
export function DropZone({
  title,
  note,
  actionLabel,
  accept,
  onFiles,
}: Readonly<{ title: string; note: string; actionLabel: string; accept: string; onFiles: (files: File[]) => void }>): JSX.Element {
  const input = useRef<HTMLInputElement>(null);
  const zone = useRef<HTMLDivElement>(null);
  // Drag and drop is added to the element itself: it is a pointer extra on a block that already has its own button.
  useEffect(() => {
    const el = zone.current;
    if (el === null) return undefined;
    const enter = (event: Event) => {
      event.preventDefault();
      el.toggleAttribute("data-over", true);
    };
    const leave = () => {
      el.toggleAttribute("data-over", false);
    };
    const drop = (event: Event) => {
      event.preventDefault();
      leave();
      onFiles([...((event as DragEvent).dataTransfer?.files ?? [])]);
    };
    el.addEventListener("dragover", enter);
    el.addEventListener("dragleave", leave);
    el.addEventListener("drop", drop);
    return () => {
      el.removeEventListener("dragover", enter);
      el.removeEventListener("dragleave", leave);
      el.removeEventListener("drop", drop);
    };
  }, [onFiles]);
  return (
    <div ref={zone} className="gx-drop">
      <b>{title}</b>
      <span className="gx-drop__note">{note}</span>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          if (input.current === null) return;
          input.current.value = ""; // the same file can be picked again
          input.current.click();
        }}
      >
        {actionLabel}
      </Button>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        accept={accept}
        aria-label={actionLabel}
        onChange={(event) => {
          onFiles([...(event.target.files ?? [])]);
        }}
      />
    </div>
  );
}

const GLYPH = {
  width: 16,
  height: 16,
  viewBox: "0 0 16 16",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  "aria-hidden": true,
} as const;

/** Two overlapping sheets: the familiar copy glyph. */
function CopyGlyph(): JSX.Element {
  return (
    <svg {...GLYPH}>
      <rect x="5.5" y="5.5" width="8" height="8" rx="1.8" />
      <path d="M10.5 3.2V3.5A1.5 1.5 0 0 0 9 2H3.5A1.5 1.5 0 0 0 2 3.5V9A1.5 1.5 0 0 0 3.5 10.5H5.5" strokeLinecap="round" />
    </svg>
  );
}

function CheckGlyph(): JSX.Element {
  return (
    <svg {...GLYPH}>
      <path d="M3 8.5L6.5 12L13 4.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** A command or an address in the mono face, with a copy icon inside the box on the right. It shows a check for about 2 s after a copy. */
export function CopyRow({
  text,
  copyLabel,
  copiedLabel,
  name,
}: Readonly<{ text: string; copyLabel: string; copiedLabel: string; name: string }>): JSX.Element {
  const [copied, setCopied] = useState(false);
  const copier = useMemo(() => createCopier((t) => navigator.clipboard.writeText(t), setCopied), []);
  useEffect(() => copier.stop, [copier]);
  return (
    <div className="gx-copyrow">
      <code className="gx-copyrow__text">{text}</code>
      <button
        type="button"
        className="gx-copyrow__btn"
        aria-label={copied ? copiedLabel : `${copyLabel} ${name}`}
        onClick={() => {
          void copier.copy(text);
        }}
      >
        {copied ? <CheckGlyph /> : <CopyGlyph />}
      </button>
      <span className="gx-sr" role="status" aria-live="polite">
        {copied ? copiedLabel : ""}
      </span>
    </div>
  );
}

/** Numbered steps. */
export function StepList({ start = 1, children }: Readonly<{ start?: number; children: ReactNode }>): JSX.Element {
  return (
    <ol className="gx-steps" start={start}>
      {children}
    </ol>
  );
}

export interface ConnectorCardProps {
  mark: VendorKind;
  name: string;
  line: string;
  /** A word in the corner: "Available", "Set up first", "For Graph". */
  tag: string;
  /** Dashed and not pressable: a connection of another stage. */
  disabled?: boolean;
  onPress: () => void;
}

/** One kind of connection in the plus dialog. */
export function ConnectorCard({ mark, name, line, tag, disabled = false, onPress }: Readonly<ConnectorCardProps>): JSX.Element {
  return (
    <button type="button" className="gx-ccard" aria-disabled={disabled ? true : undefined} onClick={disabled ? undefined : onPress}>
      <span className="gx-ccard__icon">
        <VendorMark kind={mark} />
      </span>
      <b>{name}</b>
      <small>{line}</small>
      <span className="gx-ccard__tag">{tag}</span>
    </button>
  );
}

/** The connection cards of a stage, in a grid. */
export function ConnectorGrid({ children }: Readonly<{ children: ReactNode }>): JSX.Element {
  return <div className="gx-ccards">{children}</div>;
}
