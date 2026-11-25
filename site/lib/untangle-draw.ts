import type { Palette } from "./canvas";
import { GAP_NODES, type Edge, type PageKind, type UntangleNode } from "./untangle-graph";

/** Progress at which the loose pages have turned into graph items. */
const ITEMS_AT = 0.55;
const PAD = 26;

export const easeInOut = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

type Context = CanvasRenderingContext2D;

function box(ctx: Context, p: Palette, x: number, y: number, w: number, h: number, radius = 0): void {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, radius);
  else ctx.rect(x, y, w, h);
  ctx.fillStyle = p.paper;
  ctx.fill();
  ctx.stroke();
}

function textLines(ctx: Context, p: Palette, ys: number[], x: number, widths: number[]): void {
  ctx.fillStyle = p.ink3;
  ys.forEach((y, i) => ctx.fillRect(x, y, widths[i], 1.6));
}

function polygon(ctx: Context, p: Palette, points: [number, number][]): void {
  ctx.beginPath();
  points.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
  ctx.closePath();
  ctx.fillStyle = p.paper;
  ctx.fill();
  ctx.stroke();
}

const SHEETS: Record<PageKind, (ctx: Context, p: Palette) => void> = {
  doc(ctx, p) {
    polygon(ctx, p, [
      [-8, -10],
      [3, -10],
      [8, -5],
      [8, 10],
      [-8, 10],
    ]);
    ctx.beginPath();
    ctx.moveTo(3, -10);
    ctx.lineTo(3, -5);
    ctx.lineTo(8, -5);
    ctx.stroke();
    textLines(ctx, p, [-1, 3], -5, [10, 8]);
  },
  wiki(ctx, p) {
    box(ctx, p, -8, -10, 16, 20);
    ctx.fillStyle = p.ink;
    ctx.fillRect(-8, -10, 16, 5);
    textLines(ctx, p, [-1, 3], -5, [10, 7]);
  },
  ticket(ctx, p) {
    box(ctx, p, -10, -7, 20, 14, 2.5);
    ctx.beginPath();
    ctx.moveTo(-7, 0);
    ctx.lineTo(-5, 2);
    ctx.lineTo(-1.5, -2);
    ctx.stroke();
    textLines(ctx, p, [-2.5, 1.5], 1.5, [6, 4.5]);
  },
  chat(ctx, p) {
    polygon(ctx, p, [
      [-10, -8],
      [10, -8],
      [10, 4],
      [-3, 4],
      [-8, 9],
      [-8, 4],
      [-10, 4],
    ]);
    textLines(ctx, p, [-4, -0.5], -6, [12, 8]);
  },
  notes(ctx, p) {
    box(ctx, p, -8, -8, 16, 18);
    ctx.fillStyle = p.ink;
    [-4, 0, 4].forEach((x) => ctx.fillRect(x - 0.8, -11, 1.6, 5));
    textLines(ctx, p, [-1, 3, 6.5], -5, [10, 8, 9]);
  },
  video(ctx, p) {
    box(ctx, p, -10, -7, 20, 14, 2.5);
    polygon(ctx, { ...p, paper: p.ink }, [
      [-3, -4],
      [-3, 4],
      [4.5, 0],
    ]);
  },
};

/** One page of a kind, drawn around (0, 0) in about 20 x 20; the legend icons match. */
function page(ctx: Context, p: Palette, kind: PageKind): void {
  ctx.strokeStyle = p.ink;
  ctx.lineWidth = 1.2;
  ctx.lineJoin = "round";
  SHEETS[kind](ctx, p);
}

function flag(ctx: Context, p: Palette, x: number, y: number): void {
  ctx.beginPath();
  ctx.arc(x + 10, y - 10, 8, 0, Math.PI * 2);
  ctx.fillStyle = p.ink;
  ctx.fill();
  ctx.strokeStyle = p.paper;
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.fillStyle = p.paper;
  ctx.font = `bold 11px ${p.mono}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText("!", x + 10, y - 9.5);
  ctx.textAlign = "left";
}

/** One frame at progress `t` (0 = loose pages, 1 = one graph). */
export function drawUntangle(
  ctx: Context,
  p: Palette,
  size: { w: number; h: number },
  graph: { nodes: UntangleNode[]; edges: Edge[] },
  t: number,
): void {
  const { w, h } = size;
  const { nodes, edges } = graph;
  const e = easeInOut(t);
  const place = (n: UntangleNode) => ({
    x: PAD + (n.tx + (n.ux - n.tx) * e) * (w - 2 * PAD),
    y: PAD + (n.ty + (n.uy - n.ty) * e) * (h - 2 * PAD),
  });
  ctx.fillStyle = p.paper;
  ctx.fillRect(0, 0, w, h);
  /* Links appear only once the pages have become items: before that, nothing is connected. */
  const linkAlpha = Math.max(0, (e - ITEMS_AT) / (1 - ITEMS_AT));
  if (linkAlpha > 0) {
    ctx.globalAlpha = linkAlpha;
    edges.forEach(([a, b]) => {
      const cross = nodes[a].c !== nodes[b].c;
      const from = place(nodes[a]);
      const to = place(nodes[b]);
      ctx.strokeStyle = cross ? p.ink3 : p.ink;
      ctx.lineWidth = cross ? 1.1 : 1.5;
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(to.x, to.y);
      ctx.stroke();
    });
    ctx.globalAlpha = 1;
  }
  nodes.forEach((n) => {
    const at = place(n);
    if (e < ITEMS_AT) {
      ctx.save();
      ctx.translate(at.x, at.y);
      ctx.rotate(n.rot * (1 - e * 1.8));
      page(ctx, p, n.kind);
      ctx.restore();
      return;
    }
    ctx.beginPath();
    ctx.arc(at.x, at.y, n.hub ? 9 : 5.5, 0, Math.PI * 2);
    ctx.fillStyle = n.hub ? p.ink : p.paper;
    ctx.fill();
    ctx.strokeStyle = p.ink;
    ctx.lineWidth = 1.6;
    ctx.stroke();
  });
  if (e > 0.97) GAP_NODES.forEach((k) => flag(ctx, p, place(nodes[k]).x, place(nodes[k]).y));
}
