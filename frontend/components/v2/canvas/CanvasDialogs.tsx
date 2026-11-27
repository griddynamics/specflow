"use client";
// The one dialog of the canvas. It shows the plus list of a stage, the form of one connection, a node's details, or one of the three hub
// decisions (build the graph, build the plan, the gaps). A form opens from the list, from a node (MCP) or from the URL after the Integrations
// round trip. The forms are the registry's (`connectors.ts`).
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, type ReactNode, type RefObject } from "react";
import { Dialog, Notice } from "@graphico/ui";
import { CONNECTORS } from "../connectors";
import type { ConnectorSpec, McpAddress } from "../connector-types";
import { useV2Routes } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { AddList } from "./AddDialog";
import { BuildDialog } from "./BuildDialog";
import { CANVAS_COPY } from "./canvas-copy";
import type { CanvasInput, CanvasModel, ConnectorKind, NodeView, ZoneId } from "./canvas-types";
import { GapsDialog, gapsTitle } from "./GapsDialog";
import { NodeDialog } from "./NodeDialog";
import { PlanDialog } from "./PlanDialog";

export type DialogState =
  | { type: "add"; zone: ZoneId }
  | { type: "form"; kind: ConnectorKind; zone: ZoneId }
  | { type: "node"; id: string }
  | { type: "build" }
  | { type: "plan" }
  | { type: "gaps" };

/** The plan destinations the plan goes to (Jira, GitHub Projects, Notion), by node name; null when it has none. */
export function destinationsOf(nodes: readonly Pick<NodeView, "kind" | "name">[]): string | null {
  const names = nodes.filter((n) => n.kind === "jira" || n.kind === "github_plan" || n.kind === "notion_plan").map((n) => n.name);
  return names.length === 0 ? null : names.join(", ");
}

/** The key of what the dialog shows: the focus moves when it changes (list to form to back), not when the same body is updated. */
export const bodyKeyOf = (state: DialogState | null): string | null => {
  if (state === null) return null;
  if (state.type === "form") return `form:${state.kind}`;
  return state.type === "add" ? `add:${state.zone}` : state.type;
};

const stageOf = (zone: ZoneId): string => `${CANVAS_COPY.zoneNumber[zone]} · ${CANVAS_COPY.zoneName[zone]}`;

export function CanvasDialogs({
  projectId,
  mcp,
  reload,
  input,
  model,
  state,
  onState,
  onStarted,
}: Readonly<{
  projectId: string;
  mcp: McpAddress;
  reload: RefObject<() => void>;
  input: CanvasInput;
  model: CanvasModel;
  state: DialogState | null;
  onState: (next: DialogState | null) => void;
  /** A build or a plan was just started from a dialog. */
  onStarted: { build: () => void; plan: () => void };
}>) {
  const routes = useV2Routes();
  const router = useRouter();
  const close = () => {
    onState(null);
  };
  const usable = (c: ConnectorSpec): boolean => c.app === null || input.integrations?.find((i) => i.app === c.app)?.health === "active";
  const frame = (): { title: string; eyebrow: string; body: ReactNode } | null => {
    if (state === null) return null;
    if (state.type === "add") {
      const zone = state.zone;
      return {
        title: CANVAS_COPY.addTitle[zone],
        eyebrow: stageOf(zone),
        body: (
          <>
            <AddList
              zone={zone}
              connectors={CONNECTORS}
              usable={usable}
              appName={(c) => (c.app === null ? "" : V2_COPY.appNames[c.app])}
              onPick={(c) => {
                onState({ type: "form", kind: c.kind, zone });
              }}
              onSetUp={(c) => {
                if (c.app === null) return;
                router.push(routes.integrations(projectId, { setup: c.app, next: routes.canvas(projectId, { add: c.kind }) }));
              }}
            />
            <Notice>
              {CANVAS_COPY.alwaysOn} <Link href={routes.integrations(projectId)}>{CANVAS_COPY.integrationsLink}</Link>.
            </Notice>
          </>
        ),
      };
    }
    if (state.type === "form") {
      const spec = CONNECTORS.find((c) => c.kind === state.kind);
      if (spec === undefined) return null;
      const Form = spec.Form;
      const zone = state.zone;
      return {
        title: spec.title,
        eyebrow: `${stageOf(zone)} · ${spec.zone === null ? CANVAS_COPY.mcpEyebrow : spec.name}`,
        body: (
          <Form
            projectId={projectId}
            zone={zone}
            spec={spec}
            mcp={mcp}
            reload={reload}
            onBack={() => {
              onState({ type: "add", zone });
            }}
            onClose={close}
          />
        ),
      };
    }
    return decisionFrame(state);
  };

  const decisionFrame = (s: Exclude<DialogState, { type: "add" | "form" }>) => {
    if (s.type === "node") {
      const node: NodeView | undefined = Object.values(model.zones)
        .flatMap((z) => z.nodes)
        .find((n) => n.id === s.id);
      if (node === undefined) return null;
      return {
        title: node.name,
        eyebrow: `${stageOf(node.zone)} · ${node.detail.app}`,
        body: (
          <NodeDialog
            node={node}
            projectId={projectId}
            routes={routes}
            onChanged={() => {
              reload.current();
            }}
            onClose={close}
          />
        ),
      };
    }
    if (s.type === "build" && input.status !== null) {
      const update = model.hubs.graph.decision?.label === "Update graph";
      return {
        title: update ? CANVAS_COPY.updateTitle : CANVAS_COPY.buildTitle,
        eyebrow: stageOf("graph"),
        body: <BuildDialog projectId={projectId} status={input.status} update={update} onStarted={onStarted.build} onClose={close} />,
      };
    }
    if (s.type === "plan") {
      const replan = model.hubs.plan.decision?.label === "Replan";
      const destination = destinationsOf(model.zones.plan.nodes);
      return {
        title: replan ? CANVAS_COPY.replanTitle : CANVAS_COPY.planTitle,
        eyebrow: stageOf("plan"),
        body: (
          <PlanDialog
            projectId={projectId}
            replan={replan}
            gaps={input.gaps}
            destination={destination}
            onStarted={onStarted.plan}
            onClose={close}
          />
        ),
      };
    }
    if (s.type === "gaps") {
      return {
        title: gapsTitle(input.gapRows, input.gaps),
        eyebrow: stageOf("gap"),
        body: <GapsDialog rows={input.gapRows} onClose={close} />,
      };
    }
    return null;
  };

  const shown = frame();
  const key = shown === null ? null : bodyKeyOf(state);
  // A dialog whose subject is gone (a node removed, the status not read) must not show again when the subject comes back.
  useEffect(() => {
    if (state !== null && shown === null) onState(null);
  });
  return (
    <Dialog open={shown !== null} onClose={close} title={shown?.title ?? ""} eyebrow={shown?.eyebrow} focusKey={key ?? undefined}>
      {shown?.body}
    </Dialog>
  );
}
