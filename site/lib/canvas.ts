import { useEffect } from "react";

export interface Palette {
  ink: string;
  ink3: string;
  rule: string;
  paper: string;
  mono: string;
  body: string;
}

/** A colour token resolved to a concrete colour: the tokens are `light-dark()` pairs, so only a computed `color` is usable on a canvas. */
function resolveColour(host: HTMLElement, token: string): string {
  const probe = document.createElement("span");
  probe.style.color = `var(${token})`;
  host.append(probe);
  const colour = getComputedStyle(probe).color;
  probe.remove();
  return colour;
}

/** The colours and fonts of the page theme, read from the canvas's own cascade (the `gx` root). */
export function readPalette(host: HTMLElement): Palette {
  const style = getComputedStyle(host);
  return {
    ink: resolveColour(host, "--gx-ink"),
    ink3: resolveColour(host, "--gx-ink-3"),
    rule: resolveColour(host, "--gx-rule"),
    paper: resolveColour(host, "--gx-paper"),
    mono: style.getPropertyValue("--gx-font-mono").trim() || "monospace",
    body: style.getPropertyValue("--gx-font-body").trim() || "sans-serif",
  };
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Runs `redraw` when the theme changes (the `data-theme` of <html> or the OS preference) and when the web fonts have loaded. */
export function useRedrawOnTheme(redraw: () => void): void {
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const observer = new MutationObserver(redraw);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    media.addEventListener("change", redraw);
    void document.fonts?.ready.then(redraw);
    return () => {
      observer.disconnect();
      media.removeEventListener("change", redraw);
    };
  }, [redraw]);
}

/** Sizes a canvas backing store to `width` x `height` CSS pixels at the device pixel ratio and returns the drawing context. */
export function fitCanvas(canvas: HTMLCanvasElement, width: number, height: number): CanvasRenderingContext2D | null {
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  canvas.style.height = `${height}px`;
  const context = canvas.getContext("2d");
  context?.setTransform(ratio, 0, 0, ratio, 0, 0);
  return context;
}
