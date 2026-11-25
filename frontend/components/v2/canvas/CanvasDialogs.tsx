"use client";
// The one dialog of the canvas: the plus list of a stage, or the form of one connection (its own title). A form opens from the list, from a
// node (MCP) or from the URL after the Integrations round trip. The forms are the registry's (`connectors.ts`).
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { RefObject } from "react";
import { Dialog, Notice } from "@graphico/ui";
import { CONNECTORS } from "../connectors";
import type { ConnectorSpec, McpAddress } from "../connector-types";
import { useV2Routes } from "../use-v2-data";
import type { Integration } from "@/lib/domain/integrations";
import { V2_COPY } from "../v2-copy";
import { AddList } from "./AddDialog";
import { CANVAS_COPY } from "./canvas-copy";
import type { ConnectorKind, ZoneId } from "./canvas-types";

export type DialogState = { type: "add"; zone: ZoneId } | { type: "form"; kind: ConnectorKind; zone: ZoneId };

const eyebrowOf = (zone: ZoneId, spec: ConnectorSpec | undefined): string => {
  const stage = `${CANVAS_COPY.zoneNumber[zone]} · ${CANVAS_COPY.zoneName[zone]}`;
  if (spec === undefined) return stage;
  return `${stage} · ${spec.zone === null ? CANVAS_COPY.mcpEyebrow : spec.name}`;
};

export function CanvasDialogs({
  projectId,
  mcp,
  reload,
  integrations,
  state,
  onState,
}: Readonly<{
  projectId: string;
  mcp: McpAddress;
  reload: RefObject<() => void>;
  integrations: readonly Integration[] | null;
  state: DialogState | null;
  onState: (next: DialogState | null) => void;
}>) {
  const routes = useV2Routes();
  const router = useRouter();
  const spec = state?.type === "form" ? CONNECTORS.find((c) => c.kind === state.kind) : undefined;
  const usable = (c: ConnectorSpec): boolean => c.app === null || integrations?.find((i) => i.app === c.app)?.health === "active";
  const close = () => {
    onState(null);
  };
  let body = null;
  if (state?.type === "add") {
    const zone = state.zone;
    body = (
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
    );
  } else if (state !== null && spec !== undefined) {
    const Form = spec.Form;
    const zone = state.zone;
    body = (
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
    );
  }
  return (
    <Dialog
      open={state !== null}
      onClose={close}
      title={state?.type === "add" ? CANVAS_COPY.addTitle[state.zone] : (spec?.title ?? "")}
      eyebrow={state === null ? undefined : eyebrowOf(state.zone, spec)}
    >
      {body}
    </Dialog>
  );
}
