"use client";
// Whether the caller may disconnect an app of the project: the member who connected it or the owner (AC6). The integrations list says who connected
// it; nothing is offered until it is known. An app with no connector on record (a Slack channel bound before its integration row existed) is
// removable by any member, as the API allows.
import { useEffect, useState } from "react";
import { usePorts } from "@/lib/data/provider";
import { canDisconnect, type IntegrationApp } from "@/lib/domain/integrations";

export function useRemoveRight(projectId: string, app: IntegrationApp): boolean {
  const { integrations, people, members } = usePorts();
  const [may, setMay] = useState(false);
  useEffect(() => {
    let alive = true;
    Promise.all([
      integrations.list(projectId),
      people.me().then(
        (p) => p.id,
        () => null,
      ),
      members.list(projectId).then(
        (s) => s.role === "owner",
        () => false,
      ),
    ]).then(
      ([rows, meId, isOwner]) => {
        const row = rows.find((i) => i.app === app);
        const unrecorded = row === undefined || (row.connectedBy === null && row.workspace === null);
        if (alive) setMay(unrecorded || canDisconnect(row, meId, isOwner));
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [integrations, people, members, projectId, app]);
  return may;
}
