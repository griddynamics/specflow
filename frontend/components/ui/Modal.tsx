"use client";
// FE-DS-02, FE-DOD-05.AC3, FE-DOD-05.AC4 (R5): port of `Modal` in bundle.js. On open the focus moves into the dialog and Tab
// stays inside it; Esc, the close button and the scrim call `onClose`; on close the focus returns to the opener.
// With `noAutoFocus` (embedded preview) the dialog is not modal: no trap, no Esc, no aria-modal.
// Additions to bundle.js (GAPS P04): Tab from outside the dialog is pulled back in, and `onClose` is read through a ref
// so a re-render with a new callback does not leave the Esc handler stale.
// Stack (NFR-USE-0003, sources-ux): modals open together (details, then its remove confirmation) keep a module-level stack; only the top one
// handles Esc and Tab, so one Esc closes one dialog. `closeDisabled` (a running save or removal): the close control is aria-disabled and
// Esc and the scrim do nothing.
import { useId, useRef, type ReactNode } from "react";
import { GlassSurface } from "./GlassSurface";
import { Icon } from "./Icon";
import { useModalDismissal } from "./use-outside";
import { cx, truthy } from "./utils";
import "./ui-gaps.css";

export interface ModalProps {
  open?: boolean;
  title: string;
  eyebrow?: ReactNode;
  description?: ReactNode;
  onClose?: () => void;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
  children?: ReactNode;
  /** Position inside the nearest positioned parent instead of the viewport (previews, embedded demos). */
  contained?: boolean;
  /** Keep the focus where it is on open (previews). Focus is then not returned on close either. */
  noAutoFocus?: boolean;
  /** While true, the close control is aria-disabled and Esc and the scrim are ignored (a save or a removal runs). */
  closeDisabled?: boolean;
  /** Where the focus lands on open: the first field or button (default), or the dialog itself (its title is read; for a dialog whose first button is destructive). */
  initialFocus?: "first" | "dialog";
}

export function Modal({
  open = true,
  title,
  eyebrow,
  description,
  onClose,
  footer,
  size = "md",
  children,
  contained,
  noAutoFocus,
  closeDisabled,
  initialFocus = "first",
}: Readonly<ModalProps>) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const close = () => {
    if (!truthy(closeDisabled)) onClose?.();
  };
  // Esc closes the top dialog unless a save or removal runs (`closeDisabled`); the Tab trap and the focus return are in the shared hook.
  useModalDismissal({
    ref,
    open,
    noAutoFocus: truthy(noAutoFocus),
    initialFocus,
    onEscape: () => {
      if (!truthy(closeDisabled)) onClose?.();
    },
  });

  if (!open) return null;
  return (
    <div className={cx("gdg-modal-wrap", truthy(contained) && "is-contained")}>
      <div className="gdg-modal-scrim" role="presentation" onClick={close} />
      <GlassSurface
        variant="strong"
        floating
        className={cx("gdg-modal", `gdg-modal--${size}`)}
        role="dialog"
        aria-modal={truthy(noAutoFocus) ? undefined : true}
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={ref}
      >
        <div className="gdg-modal__head">
          <div>
            {truthy(eyebrow) ? <p className="gdg-modal__eyebrow">{eyebrow}</p> : null}
            <h2 className="gdg-modal__title" id={titleId}>
              {title}
            </h2>
            {truthy(description) ? <p className="gdg-modal__desc">{description}</p> : null}
          </div>
          <button
            type="button"
            className="gdg-modal__close"
            aria-label="Close"
            aria-disabled={truthy(closeDisabled) ? true : undefined}
            onClick={close}
          >
            <Icon name="x" size={18} />
          </button>
        </div>
        <div className="gdg-modal__body" data-testid="modal-body">
          {children}
        </div>
        {truthy(footer) ? (
          <div className="gdg-modal__foot" data-testid="modal-foot">
            {footer}
          </div>
        ) : null}
      </GlassSurface>
    </div>
  );
}
