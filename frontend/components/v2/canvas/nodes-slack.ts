// Node mapper of the Slack channel: one per project (D3, D6), bound from inside Slack with the connect command. It waits for the gaps; once
// they are found it shows how many were found and answered. The wire moves only while questions go out (the gap run).
import { channelLabel } from "../integrations/slack-logic";
import { V2_COPY } from "../v2-copy";
import type { NodeMapper } from "./canvas-types";
import { connectedByOf } from "./nodes-shared";

const WORKING = new Set(["queued", "running"]);

export const slackNodes: NodeMapper = (input, stages) => {
  const channel = input.slack?.channel;
  if (channel === null || channel === undefined) return [];
  const g = input.gaps;
  const name = channelLabel(channel);
  const detail = { app: "Slack", by: connectedByOf(input, "slack", V2_COPY.connectedBy), remove: null, reconnect: null } as const;
  const base = {
    id: `slack-${channel.id}`,
    kind: "slack",
    zone: "gap",
    mark: "slack",
    name,
    progress: null,
    listing: false,
    addedAt: "",
  } as const;
  if (!stages.gapsFound || g === null) {
    const meta = V2_COPY.waitsForGaps;
    return [
      {
        ...base,
        meta,
        state: "wait",
        badge: null,
        label: `Slack channel ${name}: ${meta}`,
        wire: "off",
        detail: { ...detail, text: V2_COPY.slackPostsLater },
      },
    ];
  }
  const meta = `${String(g.total)}${g.more ? "+" : ""} found · ${String(g.answered)} answered`;
  const wire = WORKING.has(g.phase) ? "flow" : "on";
  return [
    {
      ...base,
      meta,
      state: "on",
      badge: "ok",
      label: `Slack channel ${name}: ${meta}`,
      wire,
      detail: { ...detail, text: V2_COPY.slackPosts },
    },
  ];
};
