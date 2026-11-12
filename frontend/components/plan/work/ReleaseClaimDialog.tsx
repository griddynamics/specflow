"use client";
// MCP-TOOLING session 10: the confirmation of a forced release. It says who holds the story and what happens, in the danger colour: the
// claimed tasks go back to pending, the agent must stop, its later reports on the story are refused, and unsaved agent work is not saved. The first focus lands on the dialog, not on the destructive button. Any member of the
// project may free a claim (access-control.md AC12).
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import type { Claim } from "@/lib/domain/work";
import { useWorkPort } from "../use-plan-ports";
import { personName, releaseError, taskCount } from "./work-logic";

export function ReleaseClaimDialog({
  projectId,
  claim,
  onClose,
  onDone,
}: Readonly<{ projectId: string; claim: Claim; onClose: () => void; onDone: () => void }>) {
  const work = useWorkPort();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    work.release(projectId, claim.storyId).then(
      (done) => {
        toast({ title: done.released === 0 ? "The claim was already released" : "Claim released", tone: "success" });
        onDone();
      },
      (e: unknown) => {
        const failed = releaseError(e);
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
      title="Release this claim?"
      eyebrow={claim.title}
      description={`${personName(claim.holder)} holds this story.`}
      onClose={onClose}
      closeDisabled={busy}
      initialFocus="dialog"
      footer={
        <>
          <Button variant="glass" onClick={onClose} disabled={busy}>
            Keep claim
          </Button>
          <Button onClick={submit} disabled={busy} data-testid="release-confirm">
            Release claim
          </Button>
        </>
      }
    >
      <div className="work-dialog" data-testid="release-dialog">
        <p className="work-dialog__danger">
          {`Releasing returns ${taskCount(claim.tasks)} to pending. The agent must stop working on this story. The agent will not be able to report on this story. SpecFlow does not save work the agent has not saved itself.`}
        </p>
        {error === null ? null : (
          <p className="work-dialog__error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
