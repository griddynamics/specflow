import type { Palette } from "./canvas";
import { HOPS, LINKS, NEED, SIZES, TASK, at, type Passage } from "./haystack-data";

export type Mode = "search" | "map";

const PAD = 18;
const TOP_COUNT = 10;
const MIN_WIDTH = 280;
const MIN_HEIGHT = 280;
const MAX_HEIGHT = 440;
const HEIGHT_RATIO = 0.6;
const SMALL_WIDTH = 520;

type Context = CanvasRenderingContext2D;

interface Ranked {
  k: string | null;
  d: number;
  p: { x: number; y: number };
}

export interface SearchResult {
  top: Ranked[];
  /** At 20 documents the whole project fits in the agent's context. */
  whole: boolean;
  /** Radius of the ring the search covers, in field units. */
  radius: number;
  found: Set<string>;
}

const distance = (p: { x: number; y: number }) => Math.hypot(p.x - TASK.x, p.y - TASK.y);

/** From 200 documents up, search keeps the 10 most similar passages. */
export function search(passages: Passage[], tier: number): SearchResult {
  const visible: Ranked[] = passages.filter((p) => p.tier <= tier).map((p) => ({ p, k: null, d: distance(p) }));
  const all = visible.concat(NEED.map((n) => ({ p: n, k: n.k, d: distance(n) }))).sort((a, b) => a.d - b.d);
  const whole = tier === 0;
  const top = whole ? all : all.slice(0, TOP_COUNT);
  return { top, whole, radius: whole ? 0.5 : top[top.length - 1].d, found: new Set(top.flatMap((t) => (t.k ? [t.k] : []))) };
}

/** The canvas size for the width of its box. */
export function fieldSize(boxWidth: number): { w: number; h: number } {
  const w = Math.max(MIN_WIDTH, Math.floor(boxWidth - 24));
  return { w, h: Math.round(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, w * HEIGHT_RATIO))) };
}

export function altText(mode: Mode, tier: number, whole: boolean): string {
  if (mode === "map") {
    return `Graphico graph at ${SIZES[tier]}: from the task, the agent follows links one or two hops to all five connected facts, in different documents.`;
  }
  return whole
    ? `Agent search at ${SIZES[tier]}: the whole project fits in the agent's context, so every needed fact is in it.`
    : `Agent search at ${SIZES[tier]}: the ring holds the 10 passages that look most like the task. Needed facts outside it are missed.`;
}

interface Scene {
  ctx: Context;
  p: Palette;
  w: number;
  h: number;
}

const toX = (s: Scene, x: number) => PAD + x * (s.w - 2 * PAD);
const toY = (s: Scene, y: number) => PAD + y * (s.h - 2 * PAD);

function label(s: Scene, text: string, x: number, y: number, side: number, muted: boolean): void {
  const { ctx, p, w } = s;
  ctx.font = `${w < SMALL_WIDTH ? 10.5 : 12}px ${p.mono}`;
  ctx.textBaseline = "middle";
  const width = ctx.measureText(text).width;
  const start = side > 0 ? x + 10 : side < 0 ? x - 10 - width : x - width / 2;
  const lx = Math.max(2, Math.min(w - width - 2, start));
  ctx.lineWidth = 4;
  ctx.strokeStyle = p.paper;
  ctx.lineJoin = "round";
  ctx.strokeText(text, lx, y);
  ctx.fillStyle = muted ? p.ink3 : p.ink;
  ctx.fillText(text, lx, y);
}

function dots(s: Scene, passages: Passage[], tier: number, mode: Mode, res: SearchResult): void {
  const { ctx, p } = s;
  const inTop = new Set(res.top.filter((t) => !t.k).map((t) => t.p));
  passages
    .filter((q) => q.tier <= tier)
    .forEach((q) => {
      const ring = mode === "search" && inTop.has(q);
      ctx.beginPath();
      ctx.arc(toX(s, q.x), toY(s, q.y), ring ? 4.5 : 2.2, 0, Math.PI * 2);
      if (ring) {
        ctx.fillStyle = p.paper;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = p.ink;
        ctx.stroke();
      } else {
        ctx.fillStyle = mode === "map" ? p.rule : p.ink3;
        ctx.globalAlpha = mode === "map" ? 0.9 : 0.45;
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    });
}

function searchRing(s: Scene, res: SearchResult): void {
  const { ctx, p } = s;
  ctx.save();
  ctx.setLineDash([5, 5]);
  ctx.strokeStyle = p.ink;
  ctx.lineWidth = 1.2;
  const ry = res.radius * (s.h - 2 * PAD) + 6;
  ctx.beginPath();
  ctx.ellipse(toX(s, TASK.x), toY(s, TASK.y), res.radius * (s.w - 2 * PAD) + 6, ry, 0, 0, Math.PI * 2);
  ctx.stroke();
  ctx.restore();
  label(
    s,
    res.whole ? "The whole project fits in context" : "10 most similar passages",
    toX(s, TASK.x),
    Math.max(12, toY(s, TASK.y) - ry - 10),
    0,
    false,
  );
}

/** A link is a gentle curve between two spots. */
function curve(s: Scene, a: string, b: string, bend: number) {
  const A = { x: toX(s, at(a).x), y: toY(s, at(a).y) };
  const B = { x: toX(s, at(b).x), y: toY(s, at(b).y) };
  const C = { x: (A.x + B.x) / 2 - (B.y - A.y) * bend, y: (A.y + B.y) / 2 + (B.x - A.x) * bend };
  return { A, B, C, mid: { x: (A.x + 2 * C.x + B.x) / 4, y: (A.y + 2 * C.y + B.y) / 4 }, len: Math.hypot(B.x - A.x, B.y - A.y) };
}

/** The multi-hop walk: a dashed link reads alike but has no edge. */
function walk(s: Scene): void {
  const { ctx, p } = s;
  LINKS.forEach(([a, b, , bend, alike]) => {
    const c = curve(s, a, b, bend);
    ctx.save();
    ctx.strokeStyle = p.ink;
    ctx.lineWidth = 1.6;
    if (alike) ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(c.A.x, c.A.y);
    ctx.quadraticCurveTo(c.C.x, c.C.y, c.B.x, c.B.y);
    ctx.stroke();
    ctx.restore();
  });
  Object.values(HOPS).forEach((h) => {
    ctx.beginPath();
    ctx.arc(toX(s, h.x), toY(s, h.y), 4.5, 0, Math.PI * 2);
    ctx.fillStyle = p.paper;
    ctx.fill();
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = p.ink;
    ctx.stroke();
    label(s, h.tag, toX(s, h.x), toY(s, h.y) + 16, 0, true);
  });
  if (s.w >= SMALL_WIDTH) {
    LINKS.forEach(([a, b, text, bend]) => {
      const c = curve(s, a, b, bend);
      if (c.len > 110) label(s, text, c.mid.x, c.mid.y + 13, 0, true);
    });
  }
  const m = curve(s, "R1", "R7", 0.22).mid;
  ctx.beginPath();
  ctx.arc(m.x, m.y - 14, 8, 0, Math.PI * 2);
  ctx.fillStyle = p.ink;
  ctx.fill();
  ctx.fillStyle = p.paper;
  ctx.font = `bold 11px ${p.mono}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText("!", m.x, m.y - 13.5);
  ctx.textAlign = "left";
}

function needed(s: Scene, mode: Mode, res: SearchResult): void {
  const { ctx, p } = s;
  NEED.forEach((n) => {
    const ok = mode === "map" || res.found.has(n.k);
    ctx.beginPath();
    ctx.arc(toX(s, n.x), toY(s, n.y), 6.5, 0, Math.PI * 2);
    if (ok) {
      ctx.fillStyle = p.ink;
      ctx.fill();
    } else {
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.fillStyle = p.paper;
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = p.ink;
      ctx.stroke();
      ctx.restore();
    }
    const place = s.w < SMALL_WIDTH && n.narrow ? n.narrow : { side: n.side, dy: n.dy ?? 0 };
    label(s, n.tag, toX(s, n.x), toY(s, n.y) + place.dy, place.side, !ok);
  });
  ctx.fillStyle = p.ink;
  ctx.fillRect(toX(s, TASK.x) - 7, toY(s, TASK.y) - 7, 14, 14);
  ctx.font = `bold ${s.w < SMALL_WIDTH ? 11 : 12.5}px ${p.body}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.lineWidth = 4;
  ctx.strokeStyle = p.paper;
  ctx.strokeText("Task", toX(s, TASK.x), toY(s, TASK.y) + 11);
  ctx.fillStyle = p.ink;
  ctx.fillText("Task", toX(s, TASK.x), toY(s, TASK.y) + 11);
  ctx.textAlign = "left";
}

/** One frame of the field for a method and a project size. */
export function drawHaystack(
  ctx: Context,
  p: Palette,
  size: { w: number; h: number },
  passages: Passage[],
  mode: Mode,
  tier: number,
): SearchResult {
  const s: Scene = { ctx, p, ...size };
  ctx.fillStyle = p.paper;
  ctx.fillRect(0, 0, size.w, size.h);
  const res = search(passages, tier);
  dots(s, passages, tier, mode, res);
  if (mode === "search") searchRing(s, res);
  else walk(s);
  needed(s, mode, res);
  return res;
}
