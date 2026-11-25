"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { JSX, ReactNode } from "react";
import { Button } from "./button";
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

/** A command or an address in the mono face, with a Copy button that says "Copied" for a moment. */
export function CopyRow({
  text,
  copyLabel,
  copiedLabel,
  name,
}: Readonly<{ text: string; copyLabel: string; copiedLabel: string; name: string }>): JSX.Element {
  const [copied, setCopied] = useState(false);
  const id = useId();
  return (
    <div className="gx-copyrow">
      <code id={id} className="gx-copyrow__text">
        {text}
      </code>
      <Button
        variant="ghost"
        size="sm"
        aria-label={copied ? copiedLabel : `${copyLabel} ${name}`}
        onClick={() => {
          void navigator.clipboard.writeText(text).then(
            () => {
              setCopied(true);
            },
            () => {
              setCopied(false);
            },
          );
        }}
      >
        {copied ? copiedLabel : copyLabel}
      </Button>
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
