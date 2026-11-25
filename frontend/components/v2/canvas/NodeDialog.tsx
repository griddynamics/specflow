"use client";
// A node's dialog: what it is, who set it up, in what state, and Remove for a source or a repository (after a confirmation). A stopped node
// offers to reconnect its app on the Integrations page (D2). Nothing here changes a connection: the apps are connected on that page.
import { useState } from "react";
import { Button, ButtonLink, ConfirmPanel, DialogFoot, Notice, Stack } from "@graphico/ui";
import type { RouteBuilder } from "@/lib/routes";
import { V2_COPY } from "../v2-copy";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import type { NodeView } from "./canvas-types";

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
  const { detail } = node;
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
      {failed ? <Notice role="alert">{CANVAS_COPY.removeFailed}</Notice> : null}
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
        {detail.remove !== null && !asking ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setAsking(true);
            }}
          >
            {CANVAS_COPY.remove}
          </Button>
        ) : (
          <span />
        )}
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
