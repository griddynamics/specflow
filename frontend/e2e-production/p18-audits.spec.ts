// P18 T8 (FE-DS-01.AC1, AC2, FE-DS-07.AC1, AC2, FE-DOD-04, FE-DOD-05.AC1, AC2, FE-DS-04.AC2): the P15 audits (fonts, colour tokens, keyboard
// ring walk, theme on <html>, no horizontal scroll) on the PRODUCTION routes: /login, the production Mission Control
// (empty state and Ready) and the production user menu. Stub API through the real BFF (scripts/serve-production.mjs); no model call.
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import { colourViolations, fontViolations, tabWalk } from "./audits";
import { createViaNew, expect, expectUploadReady, login, pickLocalPdf, setTheme, test } from "./support";

test.describe("P18 audits on production routes", { tag: "@production" }, () => {
  const unique = (p: string) => `${p} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;

  interface Screen {
    id: string;
    setup: (page: Page) => Promise<void>;
  }
  const SCREENS: Screen[] = [
    {
      id: "login",
      setup: async (page) => {
        await page.goto("/login");
        await expect(page.getByTestId("login-dev-email")).toBeVisible();
      },
    },
    {
      id: "production-mc-empty",
      setup: async (page) => {
        await login(page);
        await createViaNew(page, unique("Audit empty"));
      },
    },
    {
      id: "production-mc-ready",
      setup: async (page) => {
        await login(page);
        await createViaNew(page, unique("Audit ready"));
        await pickLocalPdf(page, "requirements.pdf");
        await expectUploadReady(page);
      },
    },
    {
      id: "production-user-menu",
      setup: async (page) => {
        await login(page);
        await page.getByRole("button", { name: /^Account: / }).click();
        await expect(page.getByRole("menuitem", { name: "Sign out" })).toBeVisible();
      },
    },
  ];

  for (const sc of SCREENS) {
    test(`FE-DS-01.AC1, FE-DS-04.AC2 [${sc.id}]: every visible text and form control is set in Fira Sans`, async ({ page }) => {
      await setTheme(page, "light");
      await sc.setup(page);
      expect(await fontViolations(page), "text outside Fira Sans").toEqual([]);
    });

    for (const theme of ["light", "dark"] as const) {
      test(`FE-DS-01.AC2 [${sc.id}, ${theme}]: every text colour and background is a token value of the theme`, async ({ page }) => {
        await setTheme(page, theme);
        await sc.setup(page);
        const r = await colourViolations(page);
        expect(r.tokens, "colour tokens found").toBeGreaterThan(20);
        expect(r.looked, "distinct colours looked at").toBeGreaterThanOrEqual(3);
        expect(r.bad, "colours that are not a token value").toEqual([]);
      });
    }

    test(`FE-DOD-05.AC1, AC2 [${sc.id}]: the Tab walk reaches visible stops that all show the 2px solid focus ring`, async ({ page }) => {
      await setTheme(page, "light");
      await sc.setup(page);
      const stops = await tabWalk(page);
      expect(stops.length, "the page has Tab stops (login: the email field and the submit button)").toBeGreaterThanOrEqual(2);
      expect(
        stops.filter((s) => !s.visible).map((s) => `${s.id} "${s.label}"`),
        "invisible tab stops",
      ).toEqual([]);
      expect(
        stops.filter((s) => s.ring === "none").map((s) => `${s.id} "${s.label}"`),
        "stops without the 2px focus ring",
      ).toEqual([]);
    });

    test(`FE-DOD-04 [${sc.id}]: no horizontal scroll at this viewport`, async ({ page }) => {
      await setTheme(page, "light");
      await sc.setup(page);
      const m = await page.evaluate(() => ({
        sw: document.documentElement.scrollWidth,
        cw: document.documentElement.clientWidth,
        body: document.body.scrollWidth,
      }));
      expect(m.sw).toBeLessThanOrEqual(m.cw);
      expect(m.body).toBeLessThanOrEqual(m.cw);
    });
  }

  test.describe("OS prefers dark, no theme cookie", () => {
    test.use({ colorScheme: "dark" });
    for (const sc of SCREENS) {
      test(`FE-DS-07.AC1 [${sc.id}]: <html data-theme> is light by default even with the OS in dark mode`, async ({ page }) => {
        await sc.setup(page);
        expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe("light");
      });
    }
  });

  test.describe("OS prefers light, dark cookie", () => {
    for (const sc of SCREENS) {
      test(`FE-DS-07.AC2 [${sc.id}]: the specflow-theme cookie makes <html data-theme> dark`, async ({ page }) => {
        await setTheme(page, "dark");
        await sc.setup(page);
        expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe("dark");
      });
    }
  });
});
