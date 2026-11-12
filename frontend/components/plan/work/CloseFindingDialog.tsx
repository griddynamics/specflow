"use client";
// MCP-TOOLING session 10: the dialog that resolves or dismisses one finding with an optional note. A blocking finding names the story it
// holds back. A failure shows in the dialog (role alert) and keeps the note; a finding that is gone or already answered closes the dialog
// and refreshes the list. Anyone who is a member of the project may answer (access-control.md AC12): there is no role to check here.
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { TextField } from "@/components/ui/TextField";
import { useToast } from "@/components/ui/Toast";
import type { CloseAction, Finding } from "@/lib/domain/work";
import { useWorkPort } from "../use-plan-ports";
import { blockedStory, CLOSE_COPY, closeError, NOTE_LIMIT } from "./work-logic";

export function CloseFindingDialog({
  projectId,
  finding,
  action,
  onClose,
  onDone,
}: Readonly<{ projectId: string; finding: Finding; action: CloseAction; onClose: () => void; onDone: () => void }>) {
  const work = useWorkPort();
  const toast = useToast();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const copy = CLOSE_COPY[action];
  const story = blockedStory(finding);

  const submit = () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    work.close(projectId, finding.findingId, action, note.trim() === "" ? undefined : note.trim()).then(
      () => {
        toast({ title: copy.done, tone: "success" });
        onDone();
      },
      (e: unknown) => {
        const failed = closeError(e);
        setBusy(false);
        if (failed.refresh) {
          toast({ title: failed.text, tone: "info" });
          onDone();
          return;
        }
        setError(failed.text);
      },
    );
  };

  return (
    <Modal
      title={copy.title}
      eyebrow={story ?? undefined}
      description={story === null ? undefined : "Closing it lets the story go on."}
      onClose={onClose}
      closeDisabled={busy}
      footer={
        <>
          <Button variant="glass" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={busy} data-testid="finding-confirm">
            {copy.button}
          </Button>
        </>
      }
    >
      <div className="work-dialog" data-testid="finding-dialog" data-action={action}>
        <p className="work-dialog__text">{finding.text}</p>
        <TextField
          multiline
          label="Note (optional)"
          help={copy.help}
          value={note}
          maxLength={NOTE_LIMIT}
          onChange={(e) => {
            setNote(e.target.value);
          }}
          disabled={busy}
          error={error ?? undefined}
        />
      </div>
    </Modal>
  );
}
