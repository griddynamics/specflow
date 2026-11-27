"use client";
// A node's dialog: what it is, who set it up, in what state, and Remove for a source or a repository (after a confirmation). A stopped node
// offers to reconnect its app on the Integrations page (D2). Nothing here changes a connection: the apps are connected on that page.
import { useState } from "react";
import { Button, ButtonLink, ConfirmPanel, DialogFoot, Notice, Row, Stack } from "@graphico/ui";
import type { RouteBuilder } from "@/lib/routes";
import { V2_COPY } from "../v2-copy";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import type { NodeView } from "./canvas-types";
import { NodeFiles } from "./NodeFiles";
import { RenameUpload } from "./RenameUpload";

/** The node kinds that are sources of the sources API: their files can be listed. */
const SOURCE_KINDS = ["upload", "gdrive", "confluence", "jira_epic", "linear_project", "box", "dropbox"] as const;
const sourceKindOf = (kind: NodeView["kind"]): (typeof SOURCE_KINDS)[number] | null => SOURCE_KINDS.find((k) => k === kind) ?? null;

/** The quiet actions at the left of the foot: Remove for a source or a repository, Rename for an upload (not while its field is open). */
function NodeActions({
  removable,
  renamable,
  hidden,
  renaming,
  onRemove,
  onRename,
}: Readonly<{ removable: boolean; renamable: boolean; hidden: boolean; renaming: boolean; onRemove: () => void; onRename: () => void }>) {
  const rename = renamable && !renaming;
  if (hidden || !(removable || rename)) return <span />;
  return (
    <Row>
      {removable ? (
        <Button variant="ghost" size="sm" onClick={onRemove}>
          {CANVAS_COPY.remove}
        </Button>
      ) : null}
      {rename ? (
        <Button variant="ghost" size="sm" onClick={onRename}>
          {CANVAS_COPY.rename}
        </Button>
      ) : null}
    </Row>
  );
}

export function NodeDialog({
  node,
  projectId,
  routes,
  onChanged,
  onClose,
}: Readonly<{ node: NodeView; projectId: string; routes: RouteBuilder; onChanged: () => void; onClose: () => void }>) {
  const { sources, github } = useV2Ports();
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const { detail } = node;
  const sourceKind = sourceKindOf(node.kind);
  const remove = () => {
    setBusy(true);
    const done = detail.remove === "repository" ? github.remove(projectId, node.id) : sources.remove(projectId, node.id);
    done.then(
      () => {
        onChanged();
        onClose();
      },
      () => {
        setBusy(false);
        setAsking(false);
        setFailed(true);
      },
    );
  };
  return (
    <Stack>
      <Notice>{detail.text}</Notice>
      {detail.by === null ? null : <Notice>{detail.by}</Notice>}
      {detail.link === undefined ? null : (
        <Row>
          <ButtonLink variant="ghost" size="sm" href={detail.link.href} target="_blank" rel="noopener noreferrer">
            {detail.link.label}
          </ButtonLink>
        </Row>
      )}
      {detail.open === undefined ? null : (
        <div>
          <ButtonLink variant="ghost" size="sm" href={detail.open.href} target="_blank" rel="noopener noreferrer">
            {detail.open.label}
          </ButtonLink>
        </div>
      )}
      {failed ? <Notice role="alert">{CANVAS_COPY.removeFailed}</Notice> : null}
      {renaming ? (
        <RenameUpload
          projectId={projectId}
          sourceId={node.id}
          current={node.name}
          onRenamed={() => {
            setRenaming(false);
            onChanged();
          }}
          onCancel={() => {
            setRenaming(false);
          }}
        />
      ) : null}
      {sourceKind === null ? null : <NodeFiles projectId={projectId} sourceId={node.id} kind={sourceKind} />}
      {asking ? (
        <ConfirmPanel
          title={detail.remove === "repository" ? CANVAS_COPY.removeRepoTitle : CANVAS_COPY.removeTitle}
          body={detail.remove === "repository" ? CANVAS_COPY.removeRepoBody : CANVAS_COPY.removeBody}
          confirmLabel={CANVAS_COPY.remove}
          keepLabel={CANVAS_COPY.keep}
          busy={busy}
          onKeep={() => {
            setAsking(false);
          }}
          onConfirm={remove}
        />
      ) : null}
      <DialogFoot>
        <NodeActions
          removable={detail.remove !== null}
          renamable={node.kind === "upload"}
          hidden={asking}
          renaming={renaming}
          onRemove={() => {
            setAsking(true);
          }}
          onRename={() => {
            setRenaming(true);
          }}
        />
        {detail.reconnect === null ? (
          <Button variant="ghost" onClick={onClose}>
            {CANVAS_COPY.done}
          </Button>
        ) : (
          <ButtonLink href={routes.integrations(projectId, { setup: detail.reconnect, next: routes.canvas(projectId) })}>
            {`${CANVAS_COPY.reconnect} ${V2_COPY.appNames[detail.reconnect]}`}
          </ButtonLink>
        )}
      </DialogFoot>
    </Stack>
  );
}
