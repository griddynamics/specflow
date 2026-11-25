"use client";
// `/projects/<id>`: the canvas of the project (D10, AC1). It binds the ports to the view model, the model to the geometry, and the presses to
// the one dialog. Everything the canvas shows comes from the ports; there is no endpoint of its own (AC9).
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { CONNECTORS, NODE_MAPPERS } from "../connectors";
import type { McpAddress } from "../connector-types";
import { useV2Routes } from "../use-v2-data";
import { parseCanvasQuery } from "@/lib/routes";
import { buildCanvasModel } from "./canvas-model";
import { useCanvasGeometry } from "./canvas-geometry";
import type { HubView, NodeView } from "./canvas-types";
import { CanvasDialogs, type DialogState } from "./CanvasDialogs";
import { CanvasView } from "./CanvasView";
import { useCanvasData } from "./use-canvas-data";

/** What pressing a hub opens: the decision it carries, or the gaps once the gap run has started; nothing otherwise. */
export function hubDialog(hub: HubView): DialogState | null {
  if (hub.decision !== undefined) return { type: hub.id === "graph" ? "build" : "plan" };
  return hub.id === "gap" && (hub.state === "on" || hub.state === "busy") ? { type: "gaps" } : null;
}

/** The form named by `?add=` once its app works on the project (back from Integrations); nothing else opens by itself. */
function useReturnedForm(projectId: string, onOpen: (state: DialogState) => void, usableApps: ReadonlySet<string> | null) {
  const router = useRouter();
  const routes = useV2Routes();
  const query = useSearchParams();
  const add = parseCanvasQuery(query).add;
  useEffect(() => {
    if (add === null || usableApps === null) return;
    const spec = CONNECTORS.find((c) => c.kind === add && c.zone !== null);
    if (spec?.zone != null && (spec.app === null || usableApps.has(spec.app))) onOpen({ type: "form", kind: spec.kind, zone: spec.zone });
    router.replace(routes.canvas(projectId));
  }, [add, usableApps, onOpen, router, routes, projectId]);
}

export function CanvasScreen({ mcp }: Readonly<{ mcp: McpAddress }>) {
  const { projectId } = useParams<{ projectId: string }>();
  const { input, reload } = useCanvasData(projectId);
  const model = useMemo(() => buildCanvasModel(input, NODE_MAPPERS), [input]);
  const rootRef = useRef<HTMLElement>(null);
  const geometry = useCanvasGeometry(rootRef, model.wires, model.reveal);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const usableApps = useMemo(
    () => (input.integrations === null ? null : new Set(input.integrations.filter((i) => i.health === "active").map((i) => i.app))),
    [input.integrations],
  );
  useReturnedForm(projectId, setDialog, usableApps);
  return (
    <>
      <CanvasView
        model={model}
        geometry={geometry}
        rootRef={rootRef}
        onAdd={(zone) => {
          setDialog({ type: "add", zone });
        }}
        onNode={(node: NodeView) => () => {
          setDialog(node.kind === "mcp" ? { type: "form", kind: "mcp", zone: node.zone } : { type: "node", id: node.id });
        }}
        onHub={(hub) => {
          const to = hubDialog(hub);
          return to === null
            ? undefined
            : () => {
                setDialog(to);
              };
        }}
      />
      <CanvasDialogs projectId={projectId} mcp={mcp} reload={reload} input={input} model={model} state={dialog} onState={setDialog} />
    </>
  );
}
