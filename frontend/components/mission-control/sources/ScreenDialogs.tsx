"use client";
// The dialogs of the sources screen (add link, Drive, token, details, remove, rebuild): which one is open is the screen's `Dialog` state.
import type { ToastInput } from "@/components/ui/Toast";
import { plural } from "./kb-logic";
import type { BuildMode, ProjectSourcesStatus, Source, SourcesPort } from "@/lib/domain/sources";
import { DriveDialog } from "./DriveDialog";
import { RemoveSourceDialog } from "./RemoveSourceDialog";
import { SourceDetailsModal, type GithubActions } from "./SourceDetailsModal";
import { AddLinkDialog, ConfirmDialog, TokenDialog } from "./SourceDialogs";

export type Dialog =
  | { kind: "add" }
  | { kind: "drive" }
  | { kind: "token"; sid: string; label: string }
  | { kind: "details"; sid: string }
  | { kind: "remove"; sid: string } // over the details of the same source, which stay mounted
  | { kind: "rebuild" }
  | null;

export interface ScreenDialogsProps {
  dialog: Dialog;
  onDialog: (next: Dialog) => void;
  projectId: string;
  sources: SourcesPort;
  status: ProjectSourcesStatus | null;
  /** The source whose details (or removal) are open. */
  detailsSource: Source | undefined;
  driveEmail: string | null;
  /** The member's own id (only the one who added a GitHub repository gives it a token). */
  me: string;
  toast: (t: ToastInput) => void;
  onAddLink: (url: string) => Promise<void>;
  onRemove: (src: Source) => Promise<void>;
  onRemovedElsewhere: (src: Source) => void;
  onCloseDetails: (sid: string) => void;
  onFail: (e: unknown) => void;
  onReload: () => void;
  onBuild: (mode: BuildMode) => void;
}

function githubActionsOf({
  source,
  sources,
  projectId,
  me,
  onDialog,
  onFail,
}: Readonly<Pick<ScreenDialogsProps, "sources" | "projectId" | "me" | "onDialog" | "onFail"> & { source: Source }>):
  GithubActions | undefined {
  if (!source.github) return undefined;
  return {
    onSync: () => void sources.syncNow(projectId, source.id).catch(onFail),
    onToken:
      me === source.addedBy.id
        ? () => {
            onDialog({ kind: "token", sid: source.id, label: source.name });
          }
        : null,
  };
}

export function ScreenDialogs(props: Readonly<ScreenDialogsProps>) {
  const { dialog, onDialog, projectId, sources, status, detailsSource, driveEmail, me, toast } = props;
  const close = () => {
    onDialog(null);
  };
  return (
    <>
      {dialog?.kind === "add" ? <AddLinkDialog onAdd={props.onAddLink} onClose={close} /> : null}
      {dialog?.kind === "drive" ? (
        <DriveDialog
          projectId={projectId}
          sources={sources}
          onSaved={() => {
            close();
            props.onReload(); // FR-SRC-0024: the new card, "Listing files…", is the last one at once
          }}
          onClose={close}
        />
      ) : null}
      {dialog?.kind === "token" ? (
        <TokenDialog
          target={dialog.label}
          onAdd={(token) =>
            sources.addToken(projectId, dialog.sid, token).then(() => {
              toast({ title: "Token added", message: `${dialog.label} syncs now.`, tone: "success" });
            })
          }
          onClose={close}
        />
      ) : null}
      {detailsSource ? (
        <SourceDetailsModal
          key={detailsSource.id}
          projectId={projectId}
          source={detailsSource}
          sourcesPort={sources}
          driveEmail={driveEmail}
          onClose={() => {
            close();
            props.onCloseDetails(detailsSource.id);
          }}
          onRemoveRequest={(src) => {
            onDialog({ kind: "remove", sid: src.id });
          }}
          onRemovedElsewhere={props.onRemovedElsewhere}
          github={githubActionsOf({ source: detailsSource, sources, projectId, me, onDialog, onFail: props.onFail })}
        />
      ) : null}
      {dialog?.kind === "remove" && detailsSource ? (
        <RemoveSourceDialog
          source={detailsSource}
          onCancel={() => {
            onDialog({ kind: "details", sid: detailsSource.id });
          }}
          onConfirm={() => props.onRemove(detailsSource)}
        />
      ) : null}
      {dialog?.kind === "rebuild" && status ? (
        <ConfirmDialog
          title="Rebuild from scratch?"
          description={`Specflow processes all ${plural(status.totals.documents, "document")} again. This takes longer and costs more than an update. The current knowledge stays until the rebuild is done.`}
          confirmLabel="Rebuild"
          onConfirm={() => {
            props.onBuild("rebuild");
          }}
          onClose={close}
          testId="kb-rebuild-dialog"
        />
      ) : null}
    </>
  );
}
