"use client";

import { useEffect, useId, useRef } from "react";
import type { JSX, ReactNode } from "react";

export interface DialogProps {
  open: boolean;
  /** Called when the dialog closes (Escape, the close button, or `open` going false is NOT reported). */
  onClose: () => void;
  title: string;
  /** Small mono label over the title. */
  eyebrow?: string;
  /** Accessible name of the close button. */
  closeLabel?: string;
  children?: ReactNode;
}

/** A modal dialog shell on the native `<dialog>`: title row with a close button, then the children. `open` is controlled. */
export function Dialog({ open, onClose, title, eyebrow, closeLabel = "Close", children }: Readonly<DialogProps>): JSX.Element {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog ref={ref} className="gx-dialog" aria-labelledby={titleId} onClose={onClose}>
      <div className="gx-dialog__in">
        <div className="gx-dialog__top">
          <div>
            {eyebrow === undefined ? null : <p className="gx-eyebrow gx-dialog__eyebrow">{eyebrow}</p>}
            <h2 id={titleId} className="gx-dialog__title">
              {title}
            </h2>
          </div>
          <button type="button" className="gx-dialog__close" aria-label={closeLabel} onClick={onClose}>
            ×
          </button>
        </div>
        {children}
      </div>
    </dialog>
  );
}
