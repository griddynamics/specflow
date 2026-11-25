"use client";
// docs/projects/ui.md#general-tab (layout A2): the General tab. The owner sees the name with Save, the add row with suggestions and the
// list with x; a collaborator sees the read-only list and "Leave project" (D29: no name block). What the screen shows comes from
// `MembersState.role` and `is_you`, never from `/me` or the project's creator. The rules and the port calls are in settings-logic.ts;
// the state is `use-members-controller.ts` and `use-add-row.ts`, shared with the V2 Members dialog.
import { useRouter } from "next/navigation";
import { useCallback } from "react";
import { Skeleton } from "@/components/ui/Skeleton";
import { useSettingsRoutes } from "./use-settings-ports";
import { AddRowView, GeneralView, LoadFailed } from "./parts";
import { useAddRow, type AddOutcome } from "./use-add-row";
import { useMembersController } from "./use-members-controller";

function AddRow({ projectId, onAdd }: Readonly<{ projectId: string; onAdd: (text: string) => Promise<AddOutcome> }>) {
  const row = useAddRow(projectId, onAdd);
  return (
    <AddRowView
      text={row.text}
      error={row.error}
      items={row.items}
      open={row.open}
      active={row.active}
      busy={row.busy}
      onText={row.type}
      onActive={row.setActive}
      onOpen={row.setOpen}
      onPick={row.pick}
      onAdd={row.add}
    />
  );
}

export function GeneralTab({ projectId, projectName }: Readonly<{ projectId: string; projectName: string | null }>) {
  const routes = useSettingsRoutes();
  const router = useRouter();
  const goHome = useCallback(() => {
    router.replace(routes.home()); // `/` reads the project list again (the shell too) and opens the newest remaining project
  }, [router, routes]);
  const c = useMembersController(projectId, projectName, goHome);

  if (c.state === null) {
    return (
      <div aria-hidden data-testid="settings-loading">
        <Skeleton variant="block" height={200} />
      </div>
    );
  }
  if (c.state === "failed") return <LoadFailed onRetry={c.load} />;

  return (
    <GeneralView
      state={c.state}
      draft={c.draft}
      nameError={c.nameError}
      canSave={c.canSave}
      saving={c.saving}
      confirm={c.confirm}
      confirming={c.confirming}
      addSlot={<AddRow key={c.addKey} projectId={projectId} onAdd={c.add} />}
      onDraft={c.type}
      onSave={c.save}
      onRow={c.askRow}
      onLeave={c.askLeave}
      onConfirm={c.confirmNow}
      onDismiss={c.dismiss}
    />
  );
}
