"use client";
// The Members dialog of the header (D11): what Settings > General does today, opened over the page. The body mounts only while the dialog
// is open, so it reads the members when opened and drops its state when closed. Leaving the project, or losing it meanwhile, closes the
// dialog and opens `/`, which picks the next project.
import { useRouter } from "next/navigation";
import { useCallback } from "react";
import { Button, Dialog, Row } from "@graphico/ui";
import { useMembersController } from "@/components/settings/use-members-controller";
import { useRoutes } from "@/lib/data/provider";
import { V2_COPY } from "../v2-copy";
import { MembersNotice, MembersView } from "./MembersView";

function MembersBody({ projectId, onClose }: Readonly<{ projectId: string; onClose: () => void }>) {
  const router = useRouter();
  const routes = useRoutes();
  const goHome = useCallback(() => {
    onClose();
    router.replace(routes.home());
  }, [onClose, router, routes]);
  const c = useMembersController(projectId, null, goHome);
  if (c.state === null) return <MembersNotice failed={false} />;
  if (c.state === "failed") {
    return (
      <>
        <MembersNotice failed />
        <Row>
          <Button size="sm" onClick={c.load}>
            {V2_COPY.tryAgain}
          </Button>
        </Row>
      </>
    );
  }
  return (
    <MembersView
      state={c.state}
      projectId={projectId}
      draft={c.draft}
      nameError={c.nameError}
      canSave={c.canSave}
      saving={c.saving}
      confirm={c.confirm}
      confirming={c.confirming}
      addKey={c.addKey}
      onAdd={c.add}
      onDraft={c.type}
      onSave={c.save}
      onRow={c.askRow}
      onLeave={c.askLeave}
      onConfirm={c.confirmNow}
      onDismiss={c.dismiss}
      onDone={onClose}
    />
  );
}

export function MembersDialog({ projectId, open, onClose }: Readonly<{ projectId: string; open: boolean; onClose: () => void }>) {
  return (
    <Dialog open={open} onClose={onClose} title={V2_COPY.members} eyebrow={V2_COPY.membersEyebrow}>
      {open ? <MembersBody projectId={projectId} onClose={onClose} /> : null}
    </Dialog>
  );
}
