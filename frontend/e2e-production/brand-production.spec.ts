// D21 brand polish on the PRODUCTION routes (FE-BP-03.AC5, FE-DS-08.AC1/AC2, FE-PLAN-01.AC2), against the stub API through the real BFF.
// No paid model call (R11). P1 /login title, heading and the logo pair per theme; P2 /login?signedout=1 without a session shows no note;
// P3 the cubes are on the right of the sign-in scene (the same probe as brand.spec.ts E5); P4 signed in: the title is Specflow.
// D22 P5 signed out: the apple-touch link on /login and the three icon files answer 200 without a redirect (AC2, AC3).
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import { createProjectApi, expect, login, setTheme, test, defined } from "./support";
import { expectCubesRight } from "./placement";

/**
 * The document head's `<link>` elements with this exact `rel`, as their href, sizes and type. Head metadata is not rendered and has
 * no role, label or text, so it is read from the document rather than located like page content.
 */
const headLinks = (page: Page, rel: string) =>
  page.evaluate(
    (wanted) =>
      Array.from(document.head.getElementsByTagName("link"))
        .filter((l) => l.getAttribute("rel") === wanted)
        .map((l) => ({ href: l.getAttribute("href"), sizes: l.getAttribute("sizes"), type: l.getAttribute("type") })),
    rel,
  );

/** ThemeLogo renders the light-theme file first, then the dark-theme one: the position of the logo each theme shows and hides. */
const LOGO_INDEX = { light: { shown: 0, hidden: 1 }, dark: { shown: 1, hidden: 0 } } as const;

test.describe("D21 brand polish on production routes", { tag: "@production" }, () => {
  const BLACK_H = "/ds/Logos/full-color-black-horizontal.svg";
  const WHITE_H = "/ds/Logos/full-color-white-horizontal.svg";
  const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;

  for (const theme of ["light", "dark"] as const) {
    test(`AC4, AC8, AC10 (P1): /login in the ${theme} theme has the tab title Specflow, the heading Specflow and the ${theme === "light" ? "black" : "white"}-wordmark logo`, async ({
      page,
    }) => {
      await setTheme(page, theme);
      const status: Record<string, number> = {};
      page.on("response", (r) => {
        if (r.url().includes("/ds/Logos/")) status[new URL(r.url()).pathname] = r.status();
      });
      await page.goto("/login");
      await expect(page).toHaveTitle("Specflow");
      await expect(page.getByRole("heading", { level: 1 })).toHaveText("Specflow");
      const wrapper = page.getByTestId("login-logo");
      const logos = wrapper.getByRole("img", { name: "Grid Dynamics", includeHidden: true });
      await expect(logos).toHaveCount(2);
      const shown = logos.nth(LOGO_INDEX[theme].shown);
      const hidden = logos.nth(LOGO_INDEX[theme].hidden);
      await expect(shown).toHaveClass(new RegExp(`(^| )gdg-logo-${theme}( |$)`));
      await expect(hidden).toHaveClass(new RegExp(`(^| )gdg-logo-${theme === "light" ? "dark" : "light"}( |$)`));
      await expect(shown).toBeVisible();
      await expect(shown).toHaveAttribute("src", theme === "light" ? BLACK_H : WHITE_H);
      await expect(hidden).toHaveAttribute("src", theme === "light" ? WHITE_H : BLACK_H);
      expect(await hidden.evaluate((i) => getComputedStyle(i).display)).toBe("none");
      await expect(page.getByRole("img", { name: "Grid Dynamics" }), "one logo in the accessibility tree").toHaveCount(1);
      expect(
        await shown.evaluate((i: HTMLImageElement) => ({
          h: Math.round(i.getBoundingClientRect().height),
          f: getComputedStyle(i).filter,
          nw: i.naturalWidth,
        })),
      ).toEqual({ h: 36, f: "none", nw: 2192 });
      await expect.poll(() => status[theme === "light" ? BLACK_H : WHITE_H]).toBe(200);
      // the sign-in text that stays
      await expect(page.getByTestId("login-dev-submit")).toBeVisible();
    });
  }

  test("AC10 (P2): /login?signedout=1 without a session shows no note and no alert", async ({ page }) => {
    await page.goto("/login?signedout=1");
    await expect(page).toHaveTitle("Specflow");
    await expect(page.getByTestId("login-page")).toBeVisible();
    await expect(page.getByTestId("login-dev-submit")).toBeVisible();
    await expect(page.getByTestId("login-signed-out")).toHaveCount(0);
    await expect(page.getByTestId("login-still-signed-in")).toHaveCount(0);
    await expect(page.getByTestId("login-error")).toHaveCount(0);
    await expect(
      page.getByTestId("login-page").getByRole("alert"),
      "no alert in the page (the Next route announcer sits outside it)",
    ).toHaveCount(0);
    await expect(page.getByText("You are signed out")).toHaveCount(0);
  });

  for (const theme of ["light", "dark"] as const) {
    test(`AC12, AC13 (P3): /login in the ${theme} theme: the cubes are on the right of the card`, async ({ page }) => {
      await setTheme(page, theme);
      await page.goto("/login");
      const scene = page.getByTestId("login-page");
      await expect(scene).toHaveClass(/\bgdg-scene\b/);
      const r = await expectCubesRight(page, scene);
      test.info().annotations.push({ type: "cubes /login", description: `left ${String(r.left)} right ${String(r.right)}` });
    });
  }

  test("AC8 (P4): signed in, / and a project's Mission Control have the tab title Specflow", async ({ page }) => {
    await login(page);
    await expect(page).toHaveTitle("Specflow");
    const pid = await createProjectApi(page, unique("Brand"));
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page.getByTestId("screen-mission-control")).toBeVisible();
    await expect(page).toHaveTitle("Specflow");
    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible();
    await expect(page).toHaveTitle("Specflow");
  });

  test("D22 AC2, AC3 (P5): signed out, /login has one apple-touch link and the icon files load without a redirect", async ({
    page,
    request,
  }) => {
    await page.goto("/login");
    await expect
      .poll(() => headLinks(page, "apple-touch-icon"), "one apple-touch link: /apple-icon.png, 180x180, image/png")
      .toEqual([{ href: expect.stringMatching(/^\/apple-icon\.png/), sizes: "180x180", type: "image/png" }]);
    await expect.poll(async () => (await headLinks(page, "icon")).length).toBe(2);
    const href = (await headLinks(page, "apple-touch-icon")).at(0)?.href;
    for (const [url, type] of [
      [defined(href), "image/png"],
      ["/icon.svg", "image/svg+xml"],
      ["/favicon.ico", "image/"],
    ]) {
      const res = await request.get(url, { maxRedirects: 0 });
      expect(res.status(), url).toBe(200);
      expect(res.headers()["content-type"], url).toContain(type);
    }
  });
});
