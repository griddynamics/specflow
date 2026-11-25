import type { JSX, ReactNode, Ref } from "react";
import { cx } from "../cx";
import { VendorMark } from "./vendor-mark";
import type { VendorKind } from "./vendor-mark";

/** The three stages, left to right on one line. */
export type CanvasZoneId = "graph" | "gap" | "plan";

/** `off`: not connected or the stage before it is not built (grey, dashed); `wait`: connected, waits for the stage before it (grey, solid);
 * `busy`: working now; `ready`: a person decides (the hub only); `on`: connected and working (ink); `stopped`: the API reports it stopped. */
export type CanvasState = "off" | "wait" | "busy" | "ready" | "on" | "stopped";

/** One wire: grey dashed (no data path yet), solid ink (connected), moving dashes (data moves now). */
export type WireMotion = "off" | "on" | "flow";

export interface WireView {
  key: string;
  d: string;
  motion: WireMotion;
  /** The two ends, for the little end caps. */
  ends: readonly [readonly [number, number], readonly [number, number]];
}

export interface BackdropView {
  width: number;
  height: number;
  dots: readonly { x: number; y: number; r: number; lit: boolean }[];
  lines: readonly { x1: number; y1: number; x2: number; y2: number; lit: boolean }[];
  /** Rectangles kept clear of dots and hairlines: tiles, labels. */
  holes: readonly { x: number; y: number; w: number; h: number }[];
  /** The wires' paths, kept clear too. */
  clearPaths: readonly string[];
}

/** Hub glyphs, 52 x 52: the engine of each stage. */
const HUB_GLYPHS: Record<CanvasZoneId, JSX.Element> = {
  graph: (
    <>
      <circle className="gx-fill" cx="26" cy="26" r="5" />
      <circle cx="12" cy="14" r="3.6" />
      <circle cx="40" cy="12" r="3.6" />
      <circle cx="42" cy="36" r="3.6" />
      <circle cx="14" cy="40" r="3.6" />
      <circle cx="26" cy="6.5" r="2.6" />
      <path d="M15 16l7 6.5M37.3 14.3 30 22.4M39 34.3l-8.6-5.6M17 37.8l5.3-7.4M26 9v12M15.6 14.2 23.4 7.8M14 36.4l-1.6-18.6" />
    </>
  ),
  gap: (
    <>
      <circle cx="22" cy="22" r="13" />
      <path d="m32 32 10.5 10.5" />
      <path d="M18 18.5a4.2 4.2 0 1 1 5.6 4c-1.1.4-1.6 1.2-1.6 2.3v.7" />
      <circle className="gx-fill" cx="22" cy="29.6" r="1.4" />
    </>
  ),
  plan: (
    <>
      <rect x="8" y="9" width="36" height="34" rx="5" />
      <path d="M8 18h36M17 9v34" />
      <path d="m22 25.5 2.2 2.2 4.3-4.3M32 25.5h7M22 34.5l2.2 2.2 4.3-4.3M32 34.5h7" />
    </>
  ),
};

/** The canvas: the one screen. Wires and the background graph sit under the zones. */
export function Canvas({
  label,
  rootRef,
  backdrop,
  wires,
  children,
}: Readonly<{
  label: string;
  rootRef?: Ref<HTMLElement>;
  backdrop?: BackdropView;
  wires: { width: number; height: number; items: readonly WireView[] };
  children: ReactNode;
}>): JSX.Element {
  return (
    <main className="gx-canvas" aria-label={label} ref={rootRef}>
      {backdrop === undefined ? null : <CanvasBackdrop view={backdrop} />}
      <svg className="gx-wires" aria-hidden="true" viewBox={`0 0 ${String(wires.width)} ${String(wires.height)}`}>
        <defs>
          <marker id="gx-wire-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
            <path className="gx-wire-arrow" d="M0 0 10 5 0 10z" />
          </marker>
        </defs>
        {wires.items.map((w) => (
          <g key={w.key}>
            <path className="gx-wire" data-testid={`wire-${w.key}`} data-motion={w.motion} d={w.d} />
            {w.ends.map((e, i) => (
              <circle className="gx-wire-end" data-motion={w.motion} key={i === 0 ? "from" : "to"} cx={e[0]} cy={e[1]} r="3.2" />
            ))}
          </g>
        ))}
      </svg>
      <div className="gx-zones">{children}</div>
    </main>
  );
}

function CanvasBackdrop({ view }: Readonly<{ view: BackdropView }>): JSX.Element {
  const { width, height } = view;
  return (
    <svg className="gx-backdrop" aria-hidden="true" viewBox={`0 0 ${String(width)} ${String(height)}`}>
      <defs>
        <mask id="gx-backdrop-mask" maskUnits="userSpaceOnUse" x="0" y="0" width={width} height={height}>
          <rect className="gx-mask-show" width={width} height={height} />
          {view.holes.map((h) => (
            <rect className="gx-mask-hide" key={`${String(h.x)}:${String(h.y)}`} x={h.x} y={h.y} width={h.w} height={h.h} rx="14" />
          ))}
          {view.clearPaths.map((d) => (
            <path className="gx-mask-clear" key={d} d={d} />
          ))}
        </mask>
      </defs>
      <g mask="url(#gx-backdrop-mask)">
        {view.lines.map((l) => (
          <line
            className="gx-backdrop__line"
            data-lit={l.lit}
            key={`${String(l.x1)}:${String(l.y1)}`}
            x1={l.x1}
            y1={l.y1}
            x2={l.x2}
            y2={l.y2}
          />
        ))}
        {view.dots.map((p) => (
          <circle className="gx-backdrop__dot" data-lit={p.lit} key={`${String(p.x)}:${String(p.y)}`} cx={p.x} cy={p.y} r={p.r} />
        ))}
      </g>
    </svg>
  );
}

/** One stage: its label, then its columns. */
export function CanvasZone({
  id,
  label,
  live,
  children,
}: Readonly<{ id: CanvasZoneId; label: string; live: boolean; children: ReactNode }>): JSX.Element {
  return (
    <section className="gx-zone" data-zone={id} data-live={live} aria-label={label}>
      <p className="gx-zone__label" data-hole>
        {label}
      </p>
      <div className="gx-zone__body">{children}</div>
    </section>
  );
}

const COLUMN_CLASS = { plain: "", scroll: "gx-col--scroll", row: "gx-col--row" } as const;

/** A column of a zone; `dense` packs four sources or more tighter; `row` is the gap stage's row of channels under its hub; `scroll` scrolls when the sources outgrow the screen. */
export function CanvasColumn({
  variant = "plain",
  dense = false,
  children,
}: Readonly<{ variant?: "plain" | "scroll" | "row"; dense?: boolean; children: ReactNode }>): JSX.Element {
  return <div className={cx("gx-col", COLUMN_CLASS[variant], dense && "gx-col--dense")}>{children}</div>;
}

export interface HubProps {
  id: CanvasZoneId;
  state: CanvasState;
  /** The status, in the tooltip only: a hub carries no name or count under it. */
  tooltip: string;
  /** The accessible name. */
  label: string;
  /** 0..1 fills the ring; null = no ring; "unknown" = a ring that turns while the work runs and its progress is not reported. */
  progress: number | "unknown" | null;
  /** A solid ink count on the hub, such as "3 critical". */
  pill?: string;
  /** The decision a person makes here: shown under the hub; yellow only for the first of the two. */
  decision?: { label: string; yellow: boolean };
  onPress?: () => void;
}

const RING_LENGTH = 439.8;

/** A hub: Graphico's own engine for the stage. */
export function Hub({ id, state, tooltip, label, progress, pill, decision, onPress }: Readonly<HubProps>): JSX.Element {
  const ring =
    progress === null ? null : (
      <svg className="gx-hub__ring" data-spin={progress === "unknown"} viewBox="0 0 146 146" aria-hidden="true">
        <circle
          className="gx-hub__arc"
          cx="73"
          cy="73"
          r="70"
          strokeDasharray={RING_LENGTH}
          strokeDashoffset={RING_LENGTH * (1 - (progress === "unknown" ? 0.28 : progress))}
        />
      </svg>
    );
  const inner = (
    <>
      {ring}
      <svg className="gx-hub__glyph" viewBox="0 0 52 52" aria-hidden="true">
        {HUB_GLYPHS[id]}
      </svg>
    </>
  );
  const name = decision === undefined ? label : `${label}. ${decision.label}`;
  return (
    <div className="gx-hub" data-testid={`hub-${id}`} data-state={state} data-yellow={decision?.yellow === true} data-anchor={id}>
      {onPress === undefined ? (
        <span className="gx-hub__tile" role="group" aria-label={name} title={tooltip} data-hole>
          {inner}
        </span>
      ) : (
        <button type="button" className="gx-hub__tile" aria-label={name} title={tooltip} data-hole onClick={onPress}>
          {inner}
        </button>
      )}
      {pill === undefined ? null : (
        <span className="gx-hub__pill" data-hole>
          {pill}
        </span>
      )}
      {decision === undefined ? null : (
        <span className="gx-hub__decision" data-hole>
          {decision.label}
        </span>
      )}
    </div>
  );
}

export type NodeBadge = "ok" | "spin" | "stop";

export interface CanvasNodeProps {
  id: string;
  mark: VendorKind;
  name: string;
  meta: string;
  state: CanvasState;
  badge: NodeBadge | null;
  /** 0..1 while it imports; null = no bar. */
  progress: number | null;
  /** The source still lists its files: the bar slides. */
  listing?: boolean;
  label: string;
  /** "Automatic": MCP joins by itself. */
  tag?: string;
  onPress?: () => void;
}

function Badge({ badge }: Readonly<{ badge: NodeBadge }>): JSX.Element {
  return (
    <span className="gx-cnode__badge" data-badge={badge} aria-hidden="true">
      {badge === "ok" ? (
        <svg viewBox="0 0 12 12">
          <path d="M2.5 6.2 5 8.6 9.5 3.6" />
        </svg>
      ) : null}
      {badge === "stop" ? (
        <svg viewBox="0 0 12 12">
          <path d="M6 2.5v4M6 9v.01" />
        </svg>
      ) : null}
    </span>
  );
}

/** An object of the project: one node per upload, folder, channel. Its icon shows the app, its name the object. */
export function CanvasNode(props: Readonly<CanvasNodeProps>): JSX.Element {
  const { id, mark, name, meta, state, badge, progress, listing = false, label, tag, onPress } = props;
  const inner = (
    <>
      <VendorMark kind={mark} className="gx-cnode__mark" />
      {badge === null ? null : <Badge badge={badge} />}
    </>
  );
  return (
    <div className="gx-cnode" data-testid="canvas-node" data-state={state} data-anchor={id} data-kind={mark}>
      {onPress === undefined ? (
        <span className="gx-cnode__tile" role="group" aria-label={label} data-hole>
          {inner}
        </span>
      ) : (
        <button type="button" className="gx-cnode__tile" aria-label={label} data-hole onClick={onPress}>
          {inner}
        </button>
      )}
      <span className="gx-cnode__name" title={name} data-hole>
        {name}
      </span>
      <span className="gx-cnode__meta" title={meta} data-hole>
        {meta}
      </span>
      {progress === null ? null : (
        <span className="gx-cnode__bar" data-listing={listing} aria-hidden="true">
          <i style={listing ? undefined : { width: `${String(Math.round(progress * 100))}%` }} />
        </span>
      )}
      {tag === undefined ? null : <span className="gx-cnode__tag">{tag}</span>}
    </div>
  );
}

/** The plus of a stage; the empty stage's is as large as a node. */
export function AddTile({ big, label, onPress }: Readonly<{ big: boolean; label: string; onPress: () => void }>): JSX.Element {
  return (
    <button type="button" className={cx("gx-add", big && "gx-add--big")} data-big={big} data-hole onClick={onPress}>
      <span className="gx-add__plus" aria-hidden="true">
        +
      </span>
      <span className="gx-add__label">{label}</span>
    </button>
  );
}
