// D21 brand polish on the PRODUCTION routes (FE-BP-03.AC5, FE-DS-08.AC1/AC2, FE-PLAN-01.AC2), against the stub API through the real BFF.
// No paid model call (R11). P1 the /login card: title and Graphico wordmark per theme, no Grid Dynamics logo; P2 /login?signedout=1
// without a session says "You are signed out." and stays; P4 signed in: the tab title is Graphico.
// D22 P5 signed out: the apple-touch link on /login and the three icon files answer 200 without a redirect (AC2, AC3).
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";
import { createProjectApi, expect, login, setTheme, test, defined, v2Header } from "./support";

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

test.describe("D21 brand polish on production routes", { tag: "@production" }, () => {
  const unique = (prefix: string) => `${prefix} ${Date.now().toString(36)}${randomBytes(2).toString("hex")}`;

  for (const theme of ["light", "dark"] as const) {
    test(`AC4, AC8, AC10 (P1): the /login card in the ${theme} theme has the tab title Graphico and the Graphico wordmark, no Grid Dynamics logo`, async ({
      page,
    }) => {
      await setTheme(page, theme);
      await page.goto("/login?error=x");
      await expect(page).toHaveTitle("Graphico");
      const card = page.getByTestId("login-page");
      await expect(card.getByText("Graphico", { exact: true })).toBeVisible();
      await expect(page.getByRole("heading", { level: 1 })).toHaveCount(0);
      await expect(page.getByRole("img", { name: "Grid Dynamics" })).toHaveCount(0);
      await expect(page.getByText("Specflow")).toHaveCount(0);
      await expect(page.getByRole("document")).toHaveAttribute("data-theme", theme);
      await expect(page.getByTestId("login-oidc")).toBeVisible();
    });
  }

  test("AC10 (P2): /login?signedout=1 without a session says so, waits, and shows no alert", async ({ page }) => {
    await page.goto("/login?signedout=1");
    await expect(page).toHaveTitle("Graphico");
    await expect(page.getByTestId("login-signed-out")).toHaveText("You are signed out.");
    await expect(page.getByTestId("login-oidc")).toBeVisible();
    await expect(page.getByTestId("login-still-signed-in")).toHaveCount(0);
    await expect(page.getByTestId("login-error")).toHaveCount(0);
    await expect(
      page.getByTestId("login-page").getByRole("alert"),
      "no alert in the page (the Next route announcer sits outside it)",
    ).toHaveCount(0);
    await expect(page).toHaveURL(/\/login\?signedout=1$/); // no automatic sign-in after a local-only sign-out
  });

  test("AC8 (P4): signed in, / and a project's Mission Control have the tab title Graphico", async ({ page }) => {
    await login(page);
    await expect(page).toHaveTitle("Graphico");
    const pid = await createProjectApi(page, unique("Brand"));
    await page.goto(`/projects/${pid}/mission-control`);
    await expect(page.getByTestId("screen-mission-control")).toBeVisible();
    await expect(page).toHaveTitle("Graphico");
    await page.goto("/");
    await expect(v2Header(page)).toBeVisible();
    await expect(page).toHaveTitle("Graphico");
  });

  test("D22 AC2, AC3 (P5): signed out, /login has one apple-touch link and the icon files load without a redirect", async ({
    page,
    request,
  }) => {
    await page.goto("/login?signedout=1"); // a plain /login would sign in at once; this one stays
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
