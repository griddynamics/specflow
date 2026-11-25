"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { JSX, ReactNode } from "react";
import { closeReporter, syncDialog } from "./dialog-sync";

export interface DialogProps {
  open: boolean;
  /** Called once when the person closes the dialog (Escape, the close button, or the browser). Not called when `open` goes false. The parent sets `open` false; if it keeps `open` true the dialog is shown again. */
  onClose: () => void;
  title: string;
  /** Small mono label over the title. */
  eyebrow?: string;
  /** Accessible name of the close button. */
  closeLabel?: string;
  /** False: no close button and Escape does nothing (a window the person must answer). Default true. */
  dismissible?: boolean;
  /** When it changes while the dialog stays open (a body replaces another), the focus moves to the title: the control that was pressed is gone. */
  focusKey?: string;
  children?: ReactNode;
}

/** A modal dialog shell on the native `<dialog>`: title row with a close button, then the children. `open` is controlled. */
export function Dialog({
  open,
  onClose,
  title,
  eyebrow,
  closeLabel = "Close",
  dismissible = true,
  focusKey,
  children,
}: Readonly<DialogProps>): JSX.Element {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const titleRef = useRef<HTMLHeadingElement>(null);
  const lastKey = useRef<string | undefined>(undefined);
  const [closes, setClosed] = useState(0);
  useEffect(() => {
    if (focusKey !== undefined && lastKey.current !== undefined && focusKey !== lastKey.current) titleRef.current?.focus();
    lastKey.current = focusKey;
  }, [focusKey]);
  useEffect(() => {
    if (ref.current !== null) syncDialog(ref.current, open);
  }, [open, closes]);
  const onNativeClose = useMemo(
    () =>
      closeReporter(
        () => open,
        onClose,
        () => {
          setClosed((n) => n + 1);
        },
      ),
    [open, onClose],
  );
  return (
    <dialog
      ref={ref}
      className="gx-dialog"
      aria-labelledby={titleId}
      onClose={onNativeClose}
      onCancel={
        dismissible
          ? undefined
          : (event) => {
              event.preventDefault();
            }
      }
    >
      <div className="gx-dialog__in">
        <div className="gx-dialog__top">
          <div>
            {eyebrow === undefined ? null : <p className="gx-eyebrow gx-dialog__eyebrow">{eyebrow}</p>}
            <h2 id={titleId} ref={titleRef} className="gx-dialog__title" tabIndex={-1}>
              {title}
            </h2>
          </div>
          {dismissible ? (
            <button
              type="button"
              className="gx-dialog__close"
              aria-label={closeLabel}
              onClick={() => {
                ref.current?.close();
              }}
            >
              ×
            </button>
          ) : null}
        </div>
        {children}
      </div>
    </dialog>
  );
}
