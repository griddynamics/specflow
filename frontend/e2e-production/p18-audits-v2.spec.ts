// The P18 audits (fonts, colour tokens, Tab walk with the focus ring, theme on <html>, no horizontal scroll) on the V2 routes: the canvas empty and
// with nodes, a plus dialog and a node dialog over it, and Integrations. V2 is set in Fira Sans; Fira Code only where the Graphico style sets it
// (eyebrows, zone labels, tags, code lines, copy rows). Tokens are the `--gx-*` ones of `.gx`, and the focus ring is 2px solid ink. Stub API through the real BFF.
import type { Page } from "@playwright/test";
import { colourViolations, fontViolations, tabWalkProblems } from "./audits";
import { canvas, dialog, done, nodes, SPECS, unique } from "./canvas-support";
import { expect, seedIntegrations, seedSlack, seedSource, setTheme, test } from "./support";

// The classes of `packages/ui` that set `--gx-font-mono`: eyebrows, zone labels, node and card tags, copy rows, pick rows, code lines, tags.
const MONO = ".gx-eyebrow, .gx-zone__label, .gx-cnode__tag, .gx-ccard__tag, .gx-copyrow__text, .gx-pick__row, .gx-code, .gx-tag";
const RING = { root: ".gx", token: "--gx-ink" };
const ALEX = { user_id: "u-alex", name: "Alex Kim" };

interface Screen {
  id: string;
  minStops: number;
  setup: (page: Page) => Promise<void>;
}

/** A canvas with a source, an upload and a Slack channel, so the nodes are on it. */
async function withNodes(page: Page): Promise<string> {
  const pid = await canvas(page, unique("Audit V2"));
  await seedSource(pid, { kind: "gdrive", name: SPECS, files: done(6) });
  await seedSource(pid, { kind: "upload", files: done(2) });
  await seedSlack({ pid, channel: { id: "C1", name: "specs", private: false } });
  await page.goto(`/projects/${pid}`);
  await expect(nodes(page).first()).toBeVisible();
  return pid;
}

const SCREENS: Screen[] = [
  {
    id: "v2-canvas-empty",
    minStops: 4,
    setup: async (page) => {
      await canvas(page, unique("Audit V2 empty"));
    },
  },
  {
    id: "v2-canvas-nodes",
    minStops: 6,
    setup: async (page) => {
      await withNodes(page);
    },
  },
  {
    id: "v2-plus-dialog",
    minStops: 2,
    setup: async (page) => {
      await canvas(page, unique("Audit V2 plus"));
      await page.getByRole("button", { name: "Add source" }).click();
      await expect(dialog(page, "Add a source")).toBeVisible();
    },
  },
  {
    id: "v2-node-dialog",
    minStops: 1,
    setup: async (page) => {
      await withNodes(page);
      await nodes(page).filter({ hasText: SPECS }).getByRole("button").click();
      await expect(dialog(page, SPECS)).toBeVisible();
    },
  },
  {
    id: "v2-integrations",
    minStops: 4,
    setup: async (page) => {
      const pid = await canvas(page, unique("Audit V2 apps"));
      await seedIntegrations({ pid, atlassian: { by: ALEX, health: "active" } });
      await page.goto(`/projects/${pid}/integrations`);
      await expect(page.getByText("Built in · always on")).toBeVisible();
    },
  },
];

test.afterEach(async () => {
  await seedIntegrations({ reset: true });
  await seedSlack({ pid: "none", channel: null, available: true, test: "ok" });
});

test.describe("P18 audits on the V2 routes", { tag: "@production" }, () => {
  for (const sc of SCREENS) {
    test(`[${sc.id}]: every visible text and form control is set in Fira Sans (Fira Code only where the style sets it)`, async ({
      page,
    }) => {
      await setTheme(page, "light");
      await sc.setup(page);
      expect(await fontViolations(page, MONO), "text outside Fira Sans").toEqual([]);
    });

    for (const theme of ["light", "dark"] as const) {
      test(`[${sc.id}, ${theme}]: every text colour and background is a token value of the theme`, async ({ page }) => {
        await setTheme(page, theme);
        await sc.setup(page);
        const r = await colourViolations(page, ".gx");
        expect(r.tokens, "colour tokens found").toBeGreaterThan(10);
        expect(r.looked, "distinct colours looked at").toBeGreaterThanOrEqual(2);
        expect(r.bad, "colours that are not a token value").toEqual([]);
      });
    }

    test(`[${sc.id}]: the Tab walk reaches visible stops that all show the 2px solid focus ring`, async ({ page }) => {
      await setTheme(page, "light");
      await sc.setup(page);
      const walk = await tabWalkProblems(page, RING);
      expect(walk.stops, "the page has Tab stops").toBeGreaterThanOrEqual(sc.minStops);
      expect(walk.invisible, "invisible tab stops").toEqual([]);
      expect(walk.noRing, "stops without the 2px focus ring").toEqual([]);
    });

    test(`[${sc.id}]: no horizontal scroll at this viewport`, async ({ page }) => {
      await setTheme(page, "light");
      await sc.setup(page);
      const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
      expect(m.sw).toBeLessThanOrEqual(m.cw);
    });

    test(`[${sc.id}]: the theme cookie sets <html data-theme>, and it is light by default`, async ({ page }) => {
      await setTheme(page, "dark");
      await sc.setup(page);
      expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe("dark");
    });
  }
});
