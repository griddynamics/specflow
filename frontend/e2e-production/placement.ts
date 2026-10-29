// D21 (FE-DS-08.AC2, AC12, AC13): where the cubes of a `.gdg-scene` land. Used by
// brand-production.spec.ts. Plain the runner; no app imports.
//   probe:  the scene's computed background (position "50% 50%", size "cover") and the natural size of its image (2560x720).
//   bands:  two screenshots of the scene, left of the card (L: flat ground) and right of it (R: cubes), decoded in the page through a
//           canvas; the value is the largest per-channel max-min over the band (flat ground = 0..12, cubes >= 60).
import type { Locator, Page } from "@playwright/test";
import { defined, expect } from "./support";

export const FLAT_MAX = 12;
export const CUBES_MIN = 60;
const INSET = 24; // from the scene's top, bottom and right edge, so rounded corners and edges stay out of the band
const GAP = 48; // from the card

export async function probe(page: Page, scene: Locator) {
  return scene.evaluate(async (el) => {
    const cs = getComputedStyle(el);
    // In the dark theme the scene has two layers (the scrim gradient, then the image); the image is the last one.
    const last = (v: string) => {
      // the last top-level comma-separated layer (commas inside parentheses, e.g. rgba(0, 0, 0), do not split)
      let depth = 0;
      let start = 0;
      for (let i = 0; i < v.length; i++) {
        if (v[i] === "(") depth++;
        else if (v[i] === ")") depth = Math.max(0, depth - 1);
        else if (v[i] === "," && depth === 0) start = i + 1;
      }
      return v.slice(start).trim();
    };
    const url = /url\("?([^")]+)"?\)/.exec(cs.backgroundImage)?.[1] ?? "";
    const size = await new Promise<{ w: number; h: number }>((resolve) => {
      const img = new Image();
      img.onload = () => {
        resolve({ w: img.naturalWidth, h: img.naturalHeight });
      };
      img.onerror = () => {
        resolve({ w: 0, h: 0 });
      };
      img.src = url;
    });
    return { position: last(cs.backgroundPosition), size: last(cs.backgroundSize), url, image: size };
  });
}

/** Largest per-channel (max - min) of a PNG, decoded in the page. */
export async function range(page: Page, png: Buffer): Promise<number> {
  return page.evaluate(async (b64) => {
    const must = <T>(v: T | null | undefined): T => {
      if (v === null || v === undefined) throw new Error("expected the element to exist");
      return v;
    };

    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const bmp = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
    const cv = document.createElement("canvas");
    cv.width = bmp.width;
    cv.height = bmp.height;
    const ctx = must(cv.getContext("2d"));
    ctx.drawImage(bmp, 0, 0);
    const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
    const lo = [255, 255, 255];
    const hi = [0, 0, 0];
    for (let i = 0; i < d.length; i += 4) {
      for (let c = 0; c < 3; c++) {
        if (d[i + c] < lo[c]) lo[c] = d[i + c];
        if (d[i + c] > hi[c]) hi[c] = d[i + c];
      }
    }
    return Math.max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]);
  }, png.toString("base64"));
}

type Box = { x: number; y: number; width: number; height: number } | null;

/** The box of `card`, or, when no card is given, of the scene's first glass surface (a styling class: the card has no role of its own). */
function cardBox(scene: Locator, card: Locator | undefined): Promise<Box> {
  if (card !== undefined) return card.boundingBox();
  return scene.evaluate((el) => {
    const glass = el.getElementsByClassName("gdg-glass").item(0);
    if (glass === null) return null;
    const r = glass.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  });
}

/** Waits until a box stops changing (the sidebar width animates when it expands). */
async function settled(box: () => Promise<Box>) {
  let prev = "";
  await expect
    .poll(
      async () => {
        const b = await box();
        const cur = JSON.stringify(b);
        const same = cur === prev;
        prev = cur;
        return same;
      },
      { intervals: [150], timeout: 5_000 },
    )
    .toBe(true);
}

/**
 * The scene paints the full banner with cover at 50% 50% and the cubes are on its right: the band left of the card is flat, the band right of it
 * shows cubes. `card` defaults to the scene's first glass surface. Returns the two ranges for the report.
 */
export async function expectCubesRight(page: Page, scene: Locator, card?: Locator) {
  await expect(scene).toBeVisible();
  await settled(() => scene.boundingBox());
  await settled(() => cardBox(scene, card));
  const p = await probe(page, scene);
  expect(p.position, "background-position").toBe("50% 50%");
  expect(p.size, "background-size").toBe("cover");
  expect(p.url, "backdrop url").toContain("gd-prism-cubes");
  expect(p.image, "the backdrop is the 2560x720 banner").toEqual({ w: 2560, h: 720 });
  const [s, c] = await Promise.all([scene.boundingBox(), cardBox(scene, card)]);
  expect(s && c).toBeTruthy();
  expect(
    Math.abs(defined(c).x + defined(c).width / 2 - (defined(s).x + defined(s).width / 2)),
    "the card stays centred",
  ).toBeLessThanOrEqual(1);
  const top = defined(s).y + INSET;
  const height = defined(s).height - 2 * INSET;
  const leftW = defined(c).x - GAP - (defined(s).x + INSET);
  const rightX = defined(c).x + defined(c).width + GAP;
  const rightW = defined(s).x + defined(s).width - INSET - rightX;
  expect(rightW, "room right of the card").toBeGreaterThan(48);
  let left: number | null = null;
  if (leftW >= 48) {
    left = await range(page, await page.screenshot({ clip: { x: defined(s).x + INSET, y: top, width: leftW, height } }));
    expect(left, "left of the card is flat ground").toBeLessThanOrEqual(FLAT_MAX);
  }
  const right = await range(page, await page.screenshot({ clip: { x: rightX, y: top, width: rightW, height } }));
  expect(right, "right of the card shows cubes").toBeGreaterThanOrEqual(CUBES_MIN);
  return { left, right };
}
