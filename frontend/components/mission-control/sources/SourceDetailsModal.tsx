"use client";
// FR-SRC-0005, 0008..0014, 0025, 0013 (mockup "Details 3 — grouped"): the details of one source: kind, name, who added it and when, the
// progress (or why the source failed), the files in three groups, and the footer with Remove source on the left and Close. The files
// come from `useSourceFiles` (paged, read again every 5 s). The modal stays mounted under the remove confirmation, so the fold state, the
// scroll position and the loaded pages survive Cancel. GitHub actions (demo build only) sit in the footer next to Remove source.
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Skeleton } from "@/components/ui/Skeleton";
import { sourceState } from "@/lib/domain/source-state";
import type { Source, SourcesPort } from "@/lib/domain/sources";
import { FileGroups } from "./FileGroups";
import { canSyncNow } from "./kb-logic";
import { EMPTY_SOURCE, headerText, LOAD_FAILED, type GroupId } from "./source-logic";
import { useSourceFiles, type SourceFiles } from "./use-source-files";
import { truthy } from "@/components/ui/utils";

export interface GithubActions {
  onSync: () => void;
  /** null = the member may not give access (only the one who added it can). */ onToken: (() => void) | null;
}

/** The files of the source: loading skeletons, the error with "Try again", the empty note, or the groups. */
function FilesPanel({
  files,
  counts,
  empty,
  folded,
  onToggle,
}: Readonly<{
  files: SourceFiles;
  counts: Source["counts"];
  empty: boolean;
  folded: Record<GroupId, boolean>;
  onToggle: (g: GroupId) => void;
}>) {
  if (files.phase === "loading") {
    return (
      <div className="mcs-loading" data-testid="sd-loading" aria-busy="true">
        <span className="mc-sr" role="status">
          Loading the files
        </span>
        <Skeleton variant="block" height={44} />
        <Skeleton variant="block" height={44} />
        <Skeleton variant="block" height={44} />
      </div>
    );
  }
  if (files.phase === "error") {
    return (
      <div className="mcs-details__error" role="alert">
        <p className="body" data-testid="sd-error">
          {LOAD_FAILED}
        </p>
        <Button
          variant="glass"
          size="sm"
          onClick={() => {
            files.retry();
          }}
          data-testid="sd-retry"
        >
          Try again
        </Button>
      </div>
    );
  }
  if (empty) {
    return (
      <p className="body mcs-details__empty" data-testid="sd-empty">
        {EMPTY_SOURCE}
      </p>
    );
  }
  return (
    <FileGroups
      counts={counts}
      items={files.items}
      hasMore={files.hasMore}
      loading={files.loadingMore}
      moreFailed={files.moreFailed}
      onMore={() => {
        files.loadMore();
      }}
      folded={folded}
      onToggle={onToggle}
    />
  );
}

export function SourceDetailsModal({
  projectId,
  source,
  sourcesPort,
  onClose,
  onRemoveRequest,
  onRemovedElsewhere,
  github,
  driveEmail = null,
}: Readonly<{
  projectId: string;
  source: Source;
  sourcesPort: SourcesPort;
  onClose(): void;
  onRemoveRequest(source: Source): void;
  onRemovedElsewhere(source: Source): void;
  github?: GithubActions;
  /** The Drive account email, for the "No access or not found" message (FR-GDUI-0012). */
  driveEmail?: string | null;
}>) {
  const files = useSourceFiles(sourcesPort, projectId, source.id);
  // The newest header wins: the modal's own read, else what the status read gave.
  const shown = files.source ?? source;
  const head = headerText(shown, driveEmail);
  // FR-SRC-0011: Done is folded when the modal opens; the choice holds until it closes.
  const [folded, setFolded] = useState<Record<GroupId, boolean>>({ attention: false, progress: false, done: true });

  const gone = useRef(onRemovedElsewhere);
  useEffect(() => {
    gone.current = onRemovedElsewhere;
  });
  const last = useRef(shown);
  useEffect(() => {
    last.current = shown;
  });
  useEffect(() => {
    if (files.removed) gone.current(last.current); // FR-SRC-0025.AC3
  }, [files.removed]);

  const gh = shown.github;
  const needsToken = gh !== null && (gh.state === "needs_access" || gh.state === "needs_relink") && gh.access === "github_token";
  const empty = files.phase === "ready" && shown.total === 0 && !shown.listing && !truthy(head.failure);

  return (
    <Modal
      title={shown.name}
      eyebrow={<span data-testid="sd-kind">{head.kind}</span>}
      description={<span data-testid="sd-added">{head.added}</span>}
      onClose={onClose}
      initialFocus="dialog" // the first button is Remove source: the focus starts on the dialog, whose title is read
      footer={
        <div className="mcs-foot">
          <Button
            variant="plain"
            className="mcs-danger-btn"
            onClick={() => {
              onRemoveRequest(shown);
            }}
            data-testid="sd-remove"
          >
            Remove source
          </Button>
          <span className="mcs-foot__gap" />
          {gh && needsToken && github?.onToken ? (
            <Button variant="glass" onClick={github.onToken} data-testid="sd-github-token">
              Add token
            </Button>
          ) : null}
          {gh && !needsToken && github && canSyncNow({ state: gh.state, error: gh.error }) ? (
            <Button
              variant="glass"
              onClick={() => {
                github.onSync();
              }}
              data-testid="sd-github-sync"
            >
              Sync now
            </Button>
          ) : null}
          <Button variant="glass" onClick={onClose} data-testid="sd-close">
            Close
          </Button>
        </div>
      }
    >
      <div className="mcs-details" data-testid="sd-modal" data-id={shown.id} data-state={sourceState(shown)}>
        {truthy(head.failure) ? (
          <p className="body mcs-details__failure" role="alert" data-testid="sd-failure">
            {head.failure}
          </p>
        ) : (
          <div data-testid="sd-progress">
            <ProgressBar value={head.value} label={head.progress} />
          </div>
        )}
        {gh && needsToken ? (
          <p className="body-sm mcs-details__note" data-testid="sd-github-note">
            {github?.onToken ? "Add a read-only token to sync it." : `Only ${shown.addedBy.name} can give access.`}
          </p>
        ) : null}
        <FilesPanel
          files={files}
          counts={shown.counts}
          empty={empty}
          folded={folded}
          onToggle={(g) => {
            setFolded((f) => ({ ...f, [g]: !f[g] }));
          }}
        />
      </div>
    </Modal>
  );
}
