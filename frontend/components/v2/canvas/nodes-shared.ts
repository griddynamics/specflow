// What the node mappers of an app share: the project's integration of that app, and whether it has stopped (D2). A stop is only what the
// integrations API reports (`reconnect_required`, or a binding whose member left); nothing here guesses.
import type { Integration, IntegrationApp } from "@/lib/domain/integrations";
import type { CanvasInput, NodeDetail, NodeView } from "./canvas-types";

export const integrationOf = (input: CanvasInput, app: IntegrationApp): Integration | undefined =>
  input.integrations?.find((i) => i.app === app);

/** The integration while everything that uses it is stopped; undefined while it works or was never connected. */
export function stoppedIntegration(input: CanvasInput, app: IntegrationApp): Integration | undefined {
  const i = integrationOf(input, app);
  if (i === undefined) return undefined;
  const bound = i.connectedBy !== null || i.workspace !== null;
  return i.health === "reconnect_required" || (bound && i.health === "none") ? i : undefined;
}

/** "Connected by <name>", or null when the API names nobody. */
export const connectedByOf = (input: CanvasInput, app: IntegrationApp, prefix: string): string | null => {
  const by = integrationOf(input, app)?.connectedBy;
  return by === null || by === undefined ? null : `${prefix} ${by.name}`;
};

type NodeIdentity = Pick<NodeView, "id" | "kind" | "zone" | "mark" | "name">;
type NodeWire = NodeView["wire"];

/**
 * The node states the gap and plan mappers of one app build the same way: stopped (reconnect offered when the app needs it), quiet (waiting or
 * off), busy (a pass runs) and on. `detail` is the app, who connected it and the optional link; each state adds its own text.
 */
export function nodeStates(identity: NodeIdentity, detail: Omit<NodeDetail, "text">, label: (meta: string) => string) {
  const base = { ...identity, addedAt: "", listing: false, progress: null } as const;
  const stopped = (meta: string, text: string, reconnect: IntegrationApp | null): NodeView => ({
    ...base,
    meta,
    state: "stopped",
    badge: "stop",
    label: label(meta),
    wire: "off",
    detail: { ...detail, text, reconnect },
  });
  const quiet = (meta: string, text: string = meta): NodeView => ({
    ...base,
    meta,
    state: "wait",
    badge: null,
    label: label(meta),
    wire: "off",
    detail: { ...detail, text },
  });
  const busy = (meta: string): NodeView => ({
    ...base,
    meta,
    state: "busy",
    badge: "spin",
    label: label(meta),
    wire: "flow",
    detail: { ...detail, text: meta },
  });
  const on = (meta: string, text: string, badge: NodeView["badge"], wire: NodeWire): NodeView => ({
    ...base,
    meta,
    state: "on",
    badge,
    label: label(meta),
    wire,
    detail: { ...detail, text },
  });
  return { stopped, quiet, busy, on };
}
