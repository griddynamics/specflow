// The background graph (AC10, decorative): faint dots and hairlines that light up from the Graph hub outward as the graph is built.
// Fixed seed, so every load draws the same picture. Pure: sizes and the hub position come in, a view for `@graphico/ui` comes out.
import type { BackdropView } from "@graphico/ui";

interface Point {
  x: number;
  y: number;
  /** A little jitter on the order the dots light up in. */
  j: number;
  r: number;
}

const COUNT = 60;
const SEED = 42;
const LINKS_PER_POINT = 2;
const MAX_LINK = 0.15;

function random(seed: number): () => number {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const GRAPH = (() => {
  const next = random(SEED);
  const points: Point[] = Array.from({ length: COUNT }, () => ({
    x: 0.02 + next() * 0.96,
    y: 0.04 + next() * 0.92,
    j: next(),
    r: 1.2 + next() * 0.8,
  }));
  const links: [number, number][] = [];
  points.forEach((p, i) => {
    points
      .map((q, j) => ({ j, d: (p.x - q.x) ** 2 * 3 + (p.y - q.y) ** 2 }))
      .filter((o) => o.j !== i)
      .toSorted((a, b) => a.d - b.d)
      .slice(0, LINKS_PER_POINT)
      .forEach((o) => {
        if (!links.some(([a, b]) => a === o.j && b === i)) links.push([i, o.j]);
      });
  });
  return { points, links };
})();

export interface BackdropInput {
  width: number;
  height: number;
  hub: { cx: number; cy: number } | null;
  holes: BackdropView["holes"];
  clearPaths: readonly string[];
  /** 0..1.01: the share lit; above 1 everything. */
  reveal: number;
}

export function backdropOf(input: BackdropInput): BackdropView {
  const { width: w, height: h, reveal } = input;
  const c = input.hub ?? { cx: w * 0.25, cy: h / 2 };
  const dist = GRAPH.points.map((p) => Math.hypot(p.x * w - c.cx, p.y * h - c.cy));
  const far = Math.max(...dist) || 1;
  const lit = GRAPH.points.map((p, i) => 0.85 * ((dist[i] ?? 0) / far) + 0.15 * p.j < reveal);
  const dots = GRAPH.points.map((p, i) => ({ x: p.x * w, y: p.y * h, r: p.r, lit: lit[i] }));
  const lines = GRAPH.links
    .filter(
      ([a, b]) =>
        Math.hypot(
          ((GRAPH.points[a]?.x ?? 0) - (GRAPH.points[b]?.x ?? 0)) * w,
          ((GRAPH.points[a]?.y ?? 0) - (GRAPH.points[b]?.y ?? 0)) * h,
        ) <=
        MAX_LINK * w,
    )
    .map(([a, b]) => ({
      x1: (GRAPH.points[a]?.x ?? 0) * w,
      y1: (GRAPH.points[a]?.y ?? 0) * h,
      x2: (GRAPH.points[b]?.x ?? 0) * w,
      y2: (GRAPH.points[b]?.y ?? 0) * h,
      lit: lit[a] && lit[b],
    }));
  return { width: w, height: h, dots, lines, holes: input.holes, clearPaths: input.clearPaths };
}
