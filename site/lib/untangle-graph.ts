import { seeded, shuffle } from "./rng";

export type PageKind = "doc" | "wiki" | "ticket" | "chat" | "notes" | "video";

export interface UntangleNode {
  /** Topic (cluster) index. */
  c: number;
  hub: boolean;
  /** Final place in the graph, 0..1. */
  ux: number;
  uy: number;
  /** Start place as a loose page on a grid, 0..1, and its tilt. */
  tx: number;
  ty: number;
  rot: number;
  kind: PageKind;
}

export type Edge = [number, number];

/** Four topics, each a hub with its members. */
const HUBS: [number, number][] = [
  [0.2, 0.27],
  [0.72, 0.22],
  [0.3, 0.75],
  [0.8, 0.7],
];
const MEMBERS = [7, 5, 6, 6];
const KINDS: PageKind[] = ["doc", "wiki", "ticket", "chat", "notes", "video"];
const KIND_COUNTS = [7, 5, 5, 4, 4, 3];
const GRID_COLUMNS = 7;
const GRID_ROWS = 4;
/** Nodes that carry a gap flag in the final graph. */
export const GAP_NODES = [3, 10, 24];

export interface UntangleGraph {
  nodes: UntangleNode[];
  edges: Edge[];
}

type Seed = Pick<UntangleNode, "c" | "hub" | "ux" | "uy">;

function topics(random: () => number): { seeds: Seed[]; edges: Edge[] } {
  const seeds: Seed[] = [];
  const edges: Edge[] = [];
  HUBS.forEach(([hx, hy], c) => {
    const hub = seeds.length;
    const per = MEMBERS[c];
    seeds.push({ c, hub: true, ux: hx, uy: hy });
    const a0 = random() * Math.PI * 2;
    for (let i = 0; i < per; i++) {
      const a = a0 + (i / per) * Math.PI * 2 + (random() - 0.5) * 0.7;
      const d = 0.09 + random() * 0.09;
      seeds.push({ c, hub: false, ux: hx + Math.cos(a) * d * 0.8, uy: hy + Math.sin(a) * d * 1.15 });
      edges.push([hub, hub + i + 1]);
      if (i > 0 && random() < 0.35) edges.push([hub + i, hub + i + 1]);
    }
  });
  return { seeds, edges };
}

/** Links across topics: member to member, and a few members tied straight to another topic's hub. */
function crossLinks(seeds: Seed[]): Edge[] {
  const squared = (a: Seed, b: Seed) => (a.ux - b.ux) ** 2 + (a.uy - b.uy) ** 2;
  const hubOf = (c: number) => seeds.findIndex((n) => n.c === c && n.hub);
  const near = (c: number, to: Seed, i = 0) =>
    seeds
      .map((_, k) => k)
      .filter((k) => seeds[k].c === c && !seeds[k].hub)
      .sort((x, y) => squared(seeds[x], to) - squared(seeds[y], to))[i];
  const links: Edge[] = [];
  const pairs: Edge[] = [
    [0, 1],
    [0, 2],
    [1, 3],
    [2, 3],
    [1, 2],
  ];
  pairs.forEach(([A, B]) => links.push([near(A, seeds[hubOf(B)]), near(B, seeds[hubOf(A)])]));
  const toHubs: Edge[] = [
    [0, 1],
    [3, 2],
    [1, 3],
  ];
  toHubs.forEach(([A, B]) => links.push([near(A, seeds[hubOf(B)], 1), hubOf(B)]));
  return links;
}

/** The seeded layout: every viewer sees the same pages and the same graph. */
export function buildUntangleGraph(): UntangleGraph {
  const random = seeded(11);
  const { seeds, edges } = topics(random);
  edges.push(...crossLinks(seeds));
  /* Start: every page on its own, spread on a loose grid in random order. */
  const order = shuffle(
    seeds.map((_, k) => k),
    random,
  );
  const start: Pick<UntangleNode, "tx" | "ty" | "rot">[] = [];
  order.forEach((k, i) => {
    start[k] = {
      tx: ((i % GRID_COLUMNS) + 0.5) / GRID_COLUMNS + (random() - 0.5) * 0.05,
      ty: (Math.floor(i / GRID_COLUMNS) + 0.5) / GRID_ROWS + (random() - 0.5) * 0.08,
      rot: (random() - 0.5) * 0.5,
    };
  });
  const mix = shuffle(
    KIND_COUNTS.flatMap((n, i) => Array<PageKind>(n).fill(KINDS[i])),
    random,
  );
  const nodes = seeds.map((seed, k) => ({ ...seed, ...start[k], kind: mix[k % mix.length] }));
  return { nodes, edges };
}
