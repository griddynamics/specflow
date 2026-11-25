"use client";

import { useEffect, useMemo, useRef, useState, type JSX, type RefObject } from "react";
import { Button } from "@graphico/ui";
import { fitCanvas, prefersReducedMotion, readPalette, useRedrawOnTheme } from "../lib/canvas";
import { drawUntangle } from "../lib/untangle-draw";
import { buildUntangleGraph, type UntangleGraph } from "../lib/untangle-graph";
import { PageLegend } from "./page-legend";

const STEP = 0.012;
const START_DELAY_MS = 700;
const MIN_HEIGHT = 280;
const MAX_HEIGHT = 420;
const HEIGHT_RATIO = 0.72;

const SAY_UNTANGLING = "Untangling…";
const SAY_BACK = "Putting the pages back…";
const SAY_DONE = "One connected graph. Contradictions and gaps marked.";

interface Animation {
  /** Run towards 1 (one graph) or 0 (loose pages). */
  go: (to: number) => void;
  /** Where the animation stands, 0..1. */
  progress: () => number;
  /** Draw the current frame again (theme or size changed). */
  redraw: () => void;
}

/** The animation of the hero canvas: progress moves a little each frame towards its target; reduced motion jumps. */
function useUntangle(canvas: RefObject<HTMLCanvasElement | null>, graph: UntangleGraph) {
  const [say, setSay] = useState("");
  const [atEnd, setAtEnd] = useState(false);
  const animation = useRef<Animation | null>(null);

  useEffect(() => {
    const el = canvas.current;
    if (!el) return undefined;
    let progress = 0;
    let target = 0;
    let frame = 0;
    let size = { w: 0, h: 0 };
    const redraw = () => {
      const ctx = size.w ? el.getContext("2d") : null;
      if (ctx) drawUntangle(ctx, readPalette(el), size, graph, progress);
    };
    const settle = () => {
      setSay(progress === 1 ? SAY_DONE : "");
      setAtEnd(progress === 1);
    };
    function step() {
      progress = Math.max(0, Math.min(1, progress + Math.sign(target - progress) * STEP));
      redraw();
      if (progress === target) settle();
      else frame = requestAnimationFrame(step);
    }
    const go = (to: number) => {
      target = to;
      cancelAnimationFrame(frame);
      if (prefersReducedMotion()) {
        progress = to;
        redraw();
        settle();
        return;
      }
      setSay(to ? SAY_UNTANGLING : SAY_BACK);
      frame = requestAnimationFrame(step);
    };
    animation.current = { go, progress: () => progress, redraw };

    let lastWidth = 0;
    const observer = new ResizeObserver(() => {
      const w = Math.floor(el.getBoundingClientRect().width);
      if (!w || w === lastWidth) return;
      lastWidth = w;
      size = { w, h: Math.round(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, w * HEIGHT_RATIO))) };
      fitCanvas(el, size.w, size.h);
      redraw();
    });
    observer.observe(el);
    const start = setTimeout(() => go(1), START_DELAY_MS);
    return () => {
      observer.disconnect();
      clearTimeout(start);
      cancelAnimationFrame(frame);
      animation.current = null;
    };
  }, [canvas, graph]);

  useRedrawOnTheme(() => animation.current?.redraw());
  const toggle = () => animation.current?.go(animation.current.progress() > 0.5 ? 0 : 1);
  return { say, atEnd, toggle };
}

/** Hero figure: separate document pages untangle into one graph of topic clusters, with gaps marked. */
export function Untangle(): JSX.Element {
  const canvas = useRef<HTMLCanvasElement>(null);
  const graph = useMemo(() => buildUntangleGraph(), []);
  const { say, atEnd, toggle } = useUntangle(canvas, graph);
  return (
    <figure className="panel-box" aria-label="Hundreds of pages untangled into one connected graph">
      <canvas
        ref={canvas}
        className="ut-canvas"
        role="img"
        aria-label="An animation: separate document pages turn into one graph of linked topic clusters, with its gaps marked."
      />
      <figcaption className="ut-foot">
        <PageLegend hidden={say !== ""} />
        <span className="ut-say" aria-live="polite" hidden={say === ""}>
          {say}
        </span>
        <Button variant="ghost" size="sm" onClick={toggle}>
          {atEnd ? "Start again" : "Untangle"}
        </Button>
      </figcaption>
    </figure>
  );
}
