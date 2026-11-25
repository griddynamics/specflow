// The canvas as the screen draws it: the view model and the measured geometry in, the presentational canvas of `@graphico/ui` out. No data
// access here; the screen binds ports, dialogs and presses.
import type { Ref } from "react";
import { AddTile, Canvas, CanvasColumn, CanvasNode, CanvasZone, Hub, type BackdropView, type WireView } from "@graphico/ui";
import { CANVAS_COPY } from "./canvas-copy";
import type { CanvasModel, HubView, NodeView, ZoneId, ZoneView } from "./canvas-types";

export interface CanvasViewProps {
  model: CanvasModel;
  geometry: { width: number; height: number; wires: WireView[]; backdrop: BackdropView };
  rootRef: Ref<HTMLElement>;
  onAdd: (zone: ZoneId) => void;
  /** A node's press, or `undefined` for a node that opens nothing. */
  onNode: (node: NodeView) => (() => void) | undefined;
  /** A hub's press, or `undefined` for a hub that opens nothing. */
  onHub: (hub: HubView) => (() => void) | undefined;
}

function Nodes({ nodes, onNode }: Readonly<{ nodes: readonly NodeView[]; onNode: CanvasViewProps["onNode"] }>) {
  return nodes.map((n) => (
    <CanvasNode
      key={n.id}
      id={n.id}
      mark={n.mark}
      name={n.name}
      meta={n.meta}
      state={n.state}
      badge={n.badge}
      progress={n.progress}
      listing={n.listing}
      label={n.label}
      tag={n.tag}
      onPress={onNode(n)}
    />
  ));
}

function ZoneHub({ hub, onHub }: Readonly<{ hub: HubView; onHub: CanvasViewProps["onHub"] }>) {
  return (
    <CanvasColumn>
      <Hub
        id={hub.id}
        state={hub.state}
        tooltip={hub.tooltip}
        label={hub.label}
        progress={hub.progress}
        pill={hub.pill}
        decision={hub.decision}
        onPress={onHub(hub)}
      />
    </CanvasColumn>
  );
}

function Add({ zone, onAdd }: Readonly<{ zone: ZoneView; onAdd: CanvasViewProps["onAdd"] }>) {
  return (
    <AddTile
      big={zone.bigAdd}
      label={zone.addLabel}
      onPress={() => {
        onAdd(zone.id);
      }}
    />
  );
}

export function CanvasView({ model, geometry, rootRef, onAdd, onNode, onHub }: Readonly<CanvasViewProps>) {
  const { graph, gap, plan } = model.zones;
  return (
    <Canvas
      label={CANVAS_COPY.canvasLabel}
      rootRef={rootRef}
      backdrop={geometry.backdrop}
      wires={{ width: geometry.width, height: geometry.height, items: geometry.wires }}
    >
      <CanvasZone id="graph" label={graph.label} live={graph.live}>
        <CanvasColumn variant="scroll">
          <Nodes nodes={graph.nodes} onNode={onNode} />
          <Add zone={graph} onAdd={onAdd} />
        </CanvasColumn>
        <ZoneHub hub={model.hubs.graph} onHub={onHub} />
      </CanvasZone>
      <CanvasZone id="gap" label={gap.label} live={gap.live}>
        <ZoneHub hub={model.hubs.gap} onHub={onHub} />
        <CanvasColumn variant="row">
          <Nodes nodes={gap.nodes} onNode={onNode} />
          <Add zone={gap} onAdd={onAdd} />
        </CanvasColumn>
      </CanvasZone>
      <CanvasZone id="plan" label={plan.label} live={plan.live}>
        <ZoneHub hub={model.hubs.plan} onHub={onHub} />
        <CanvasColumn>
          <Nodes nodes={plan.nodes} onNode={onNode} />
          <Add zone={plan} onAdd={onAdd} />
        </CanvasColumn>
      </CanvasZone>
    </Canvas>
  );
}
