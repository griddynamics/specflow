"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from "react";
import { fitCanvas, readPalette, useRedrawOnTheme } from "../lib/canvas";
import { NEED, SIZES, buildPassages } from "../lib/haystack-data";
import { altText, drawHaystack, fieldSize, search, type Mode } from "../lib/haystack-draw";
import { HaystackLegend, HaystackReadout } from "./haystack-readout";

/** Context for coding agents: a needle in a haystack. Seeded, so every viewer sees the same field. */
export function Haystack(): JSX.Element {
  const field = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const passages = useMemo(() => buildPassages(), []);
  const [mode, setMode] = useState<Mode>("search");
  const [tier, setTier] = useState(2);
  const [width, setWidth] = useState(0);
  const res = useMemo(() => search(passages, tier), [passages, tier]);

  const draw = useCallback(() => {
    const el = canvas.current;
    const box = field.current;
    if (!el || !box) return;
    const size = fieldSize(box.getBoundingClientRect().width);
    const ctx = fitCanvas(el, size.w, size.h);
    if (ctx) drawHaystack(ctx, readPalette(el), size, passages, mode, tier);
  }, [passages, mode, tier]);

  useEffect(() => {
    const box = field.current;
    if (!box) return undefined;
    const observer = new ResizeObserver(() => setWidth(Math.floor(box.getBoundingClientRect().width)));
    observer.observe(box);
    return () => observer.disconnect();
  }, []);

  // Draw after every change of method, size, width and theme.
  useEffect(() => {
    draw();
  }, [draw, width]);
  useRedrawOnTheme(draw);

  return (
    <div className="panel-box" id="haystack" aria-label="Illustration: what a coding agent gets from search and from Graphico">
      <div className="hs-bar">
        <div className="seg" role="group" aria-label="Method">
          <button type="button" aria-pressed={mode === "search"} onClick={() => setMode("search")}>
            Agent search
          </button>
          <button type="button" aria-pressed={mode === "map"} onClick={() => setMode("map")}>
            Graphico graph
          </button>
        </div>
        <label className="hs-size" htmlFor="hs-size">
          Project size
          <input
            type="range"
            id="hs-size"
            min={0}
            max={SIZES.length - 1}
            step={1}
            value={tier}
            aria-valuetext={SIZES[tier]}
            onChange={(e) => setTier(Number(e.target.value))}
          />
          <output id="hs-size-out" htmlFor="hs-size">
            {SIZES[tier]}
          </output>
        </label>
      </div>
      <div className="hs-body">
        <div className="hs-field" ref={field}>
          <canvas ref={canvas} role="img" aria-label={altText(mode, tier, res.whole)} />
          <HaystackLegend mode={mode} />
        </div>
        <HaystackReadout mode={mode} res={res} needed={NEED} />
      </div>
    </div>
  );
}
