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

/** The two resting looks of a plan destination's node: stopped (its text, and the app to reconnect) and quiet (waiting, the wire off). */
export function restingViews(
  app: IntegrationApp,
  base: Omit<NodeView, "meta" | "state" | "badge" | "label" | "wire" | "detail">,
  detail: Omit<NodeDetail, "text" | "reconnect">,
  label: (meta: string) => string,
) {
  const stopped = (meta: string, text: string, reconnect: typeof app | null): NodeView => ({
    ...base,
    meta,
    state: "stopped",
    badge: "stop",
    label: label(meta),
    wire: "off",
    detail: { ...detail, text, reconnect },
  });
  const quiet = (meta: string): NodeView => ({
    ...base,
    meta,
    state: "wait",
    badge: null,
    label: label(meta),
    wire: "off",
    detail: { ...detail, text: meta, reconnect: null },
  });
  return { stopped, quiet };
}
