import type { JSX } from "react";
import { Button } from "./button";

export interface ConfirmPanelProps {
  title: string;
  body: string;
  confirmLabel: string;
  keepLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onKeep: () => void;
}

/** "Are you sure" inside a dialog: the one thing asked, a quiet Keep and the action. Keep has the focus. */
export function ConfirmPanel({
  title,
  body,
  confirmLabel,
  keepLabel,
  busy = false,
  onConfirm,
  onKeep,
}: Readonly<ConfirmPanelProps>): JSX.Element {
  return (
    <div className="gx-confirm" role="alertdialog" aria-label={title}>
      <b>{title}</b>
      <p>{body}</p>
      <div className="gx-row">
        {/* The panel replaces the actions it confirms, so the quiet choice takes the focus. */}
        <Button variant="ghost" size="sm" onClick={onKeep} autoFocus>
          {keepLabel}
        </Button>
        <Button size="sm" onClick={onConfirm} disabled={busy}>
          {confirmLabel}
        </Button>
      </div>
    </div>
  );
}
