"use client";
// FR-SRC-0015, 0019, 0023 (mockup "Remove source — confirmation"): the confirmation over the details modal. Remove source stays off until the
// field holds the exact full source name (spaces at both ends are ignored). While the removal runs, up to 60 s, the button is busy and Esc,
// Cancel, the scrim and the close control do nothing; a failure or no answer keeps the dialog open with the failure text and the button
// available again. Cancel, Esc and the close control give the focus back to the details modal and remove nothing.
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { TextField } from "@/components/ui/TextField";
import type { Source } from "@/lib/domain/sources";
import { settle } from "./drive-logic";
import { nameMatches, REMOVE_CUTOFF_MS, REMOVE_FAILED, REMOVE_TITLE, removeLabel, removeWarning } from "./source-logic";

export function RemoveSourceDialog({
  source,
  onCancel,
  onConfirm,
}: Readonly<{ source: Source; onCancel(): void; onConfirm(): Promise<void> }>) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const ok = nameMatches(typed, source);
  const [before, name, after] = removeWarning(source);

  async function remove() {
    if (busy || !ok) return;
    setBusy(true);
    setFailed(false);
    const r = await settle(onConfirm(), REMOVE_CUTOFF_MS);
    if (!alive.current) return; // removed: the screen closed this dialog
    if (r.kind === "ok") return;
    setBusy(false);
    setFailed(true);
  }

  const off = busy || !ok;
  return (
    <Modal
      title={REMOVE_TITLE}
      size="sm"
      onClose={onCancel}
      closeDisabled={busy}
      footer={
        <>
          <Button
            variant="glass"
            onClick={() => {
              if (!busy) onCancel();
            }}
            aria-disabled={busy ? true : undefined}
            className={busy ? "mcs-btn--off" : undefined}
            data-testid="mcs-remove-cancel"
          >
            Cancel
          </Button>
          <Button
            onClick={() => void remove()}
            aria-disabled={off ? true : undefined}
            aria-busy={busy || undefined}
            className={off ? "mcs-btn--off" : undefined}
            data-testid="mcs-remove-dialog-confirm"
          >
            Remove source
          </Button>
        </>
      }
    >
      <div className="mcs-remove" data-testid="mcs-remove-dialog">
        <div className="mcs-remove__warn">
          <Icon name="warning-circle" size={20} />
          <p className="body-sm">
            {before}
            <b>{name}</b>
            {after}
          </p>
        </div>
        <TextField
          label={removeLabel(source)}
          value={typed}
          readOnly={busy}
          spellCheck={false}
          onChange={(e) => {
            setTyped(e.target.value);
            if (failed) setFailed(false);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void remove();
            }
          }}
          data-testid="mcs-remove-name"
        />
        {failed ? (
          <p className="body-sm mcs-remove__error" role="alert" data-testid="mcs-remove-error">
            {REMOVE_FAILED}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
