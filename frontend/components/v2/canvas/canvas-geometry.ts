"use client";
// The one module that measures the DOM (exempt from `styling-layout-measurement`, frontend/semgrep/ds-styling.yml): the wires between the
// nodes and hubs and the clear spots of the background graph are drawn from the boxes the browser laid out, which CSS cannot state.
// Everything else on the canvas is placed by `packages/ui/src/styles/canvas.css`. `wirePath` is pure and tested; the hook measures.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import type { BackdropView, WireView } from "@graphico/ui";
import { backdropOf } from "./canvas-backdrop";
import type { WireSpec } from "./canvas-types";

export interface Box {
  l: number;
  r: number;
  t: number;
  b: number;
  cx: number;
  cy: number;
}

/** The little gap between a wire's end cap and the tile it points at. */
const GAP = 6;
const MIN_REACH = 30;
/** Rooms kept clear around a tile or label. */
const HOLE_PAD = 16;

type Point = readonly [number, number];

/** A curve from tile `a` to tile `b`: sideways when `b` is to the right of `a`, else straight down (or up); `down` forces it vertical. */
export function wirePath(a: Box, b: Box, down: boolean): { d: string; ends: readonly [Point, Point] } {
  if (!down && b.l >= a.r - 4) {
    const s: Point = [a.r + GAP, a.cy];
    const e: Point = [b.l - GAP, b.cy];
    const dx = Math.max(MIN_REACH, (e[0] - s[0]) / 2);
    return {
      d: `M${String(s[0])} ${String(s[1])} C${String(s[0] + dx)} ${String(s[1])} ${String(e[0] - dx)} ${String(e[1])} ${String(e[0])} ${String(e[1])}`,
      ends: [s, e],
    };
  }
  const below = b.t > a.b;
  const s: Point = [a.cx, below ? a.b + GAP : a.t - GAP];
  const e: Point = [b.cx, below ? b.t - GAP : b.b + GAP];
  const dy = (e[1] - s[1]) / 2;
  return {
    d: `M${String(s[0])} ${String(s[1])} C${String(s[0])} ${String(s[1] + dy)} ${String(e[0])} ${String(e[1] - dy)} ${String(e[0])} ${String(e[1])}`,
    ends: [s, e],
  };
}

interface Measured {
  width: number;
  height: number;
  wires: WireView[];
  holes: BackdropView["holes"];
  hub: { cx: number; cy: number } | null;
}

const EMPTY: Measured = { width: 1, height: 1, wires: [], holes: [], hub: null };

const boxOf = (el: Element, root: DOMRect): Box => {
  const r = el.getBoundingClientRect();
  return {
    l: r.left - root.left,
    r: r.right - root.left,
    t: r.top - root.top,
    b: r.bottom - root.top,
    cx: (r.left + r.right) / 2 - root.left,
    cy: (r.top + r.bottom) / 2 - root.top,
  };
};

function measure(root: HTMLElement, specs: readonly WireSpec[]): Measured {
  const rect = root.getBoundingClientRect();
  const tile = (id: string): Element | null => root.querySelector(`[data-anchor="${CSS.escape(id)}"] [data-hole]`);
  const wires: WireView[] = [];
  for (const spec of specs) {
    const from = tile(spec.from);
    const to = tile(spec.to);
    if (from === null || to === null) continue;
    const { d, ends } = wirePath(boxOf(from, rect), boxOf(to, rect), spec.down);
    wires.push({ key: spec.key, d, motion: spec.motion, ends });
  }
  const holes = [...root.querySelectorAll("[data-hole]")]
    .filter((el) => el.getClientRects().length > 0)
    .map((el) => boxOf(el, rect))
    .map((q) => ({ x: q.l - HOLE_PAD, y: q.t - HOLE_PAD, w: q.r - q.l + 2 * HOLE_PAD, h: q.b - q.t + 2 * HOLE_PAD }));
  const hubTile = tile("graph");
  const hub = hubTile === null ? null : boxOf(hubTile, rect);
  return {
    width: Math.max(1, root.clientWidth),
    height: Math.max(1, root.clientHeight),
    wires,
    holes,
    hub: hub === null ? null : { cx: hub.cx, cy: hub.cy },
  };
}

/**
 * Measures the canvas `root` and returns the wires and the background graph for the current layout. It measures again when the canvas
 * resizes, when a column scrolls, when the fonts arrive, and when the wires to draw change. State is set only when the picture changes.
 */
export function useCanvasGeometry(root: RefObject<HTMLElement | null>, specs: readonly WireSpec[], reveal: number) {
  const [measured, setMeasured] = useState<Measured>(EMPTY);
  const last = useRef("");
  const frame = useRef(0);
  const specsRef = useRef(specs);
  specsRef.current = specs;
  const key = specs.map((s) => `${s.key}:${s.from}>${s.to}:${s.motion}`).join("|");

  const run = useCallback(() => {
    const el = root.current;
    if (el === null) return;
    const next = measure(el, specsRef.current);
    const text = JSON.stringify(next);
    if (text === last.current) return;
    last.current = text;
    setMeasured(next);
  }, [root]);
  const schedule = useCallback(() => {
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(run);
  }, [run]);

  useLayoutEffect(() => {
    run();
  }, [run, key]);
  useEffect(() => {
    const el = root.current;
    if (el === null) return undefined;
    const observer = new ResizeObserver(schedule);
    observer.observe(el);
    el.addEventListener("scroll", schedule, { capture: true, passive: true });
    void document.fonts.ready.then(schedule);
    return () => {
      observer.disconnect();
      el.removeEventListener("scroll", schedule, { capture: true });
      cancelAnimationFrame(frame.current);
    };
  }, [root, schedule]);

  const backdrop = useMemo(
    () =>
      backdropOf({
        width: measured.width,
        height: measured.height,
        hub: measured.hub,
        holes: measured.holes,
        clearPaths: measured.wires.map((w) => w.d),
        reveal,
      }),
    [measured, reveal],
  );
  return { width: measured.width, height: measured.height, wires: measured.wires, backdrop };
}
