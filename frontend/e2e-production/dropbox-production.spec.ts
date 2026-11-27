// Dropbox on the PRODUCTION routes in a real browser, against the stub API through the real BFF (docs/dropbox/oauth.md, docs/dropbox/source.md): the Dropbox
// card of the Integrations page (not set up, connected, expired), Connect going to Dropbox's consent, and the canvas "Dropbox link" form (Set up first, the check
// with each closed refusal in words, the file list with unchecking, Add source with `exclude`) and its node (importing, done, stopped). Dropbox's own pages are
// intercepted and never reached. Screenshots of the states go to the folder in `DROPBOX_SCREENS` when it is set (never inside the repository).
import path from "node:path";
import type { Page } from "@playwright/test";
import { ADD, canvas, checkPickAndAdd, dialog, nodes, STATE, unique } from "./canvas-support";
import {
  allowConsoleErrors,
  allowOrigin,
  expect,
  loginV2,
  seedDropbox,
  seedIntegrations,
  seedMembers,
  seedSource,
  stubLog,
  test,
  v2Header,
} from "./support";

const ALEX = { user_id: "someone", name: "Sam Ortiz" };
const ACME = { id: "team-stub", name: "Acme Team" };
const SET_UP_FIRST = "Set up first";
const CHECK_LINK = "Check link";
const FOLDER = "https://www.dropbox.com/home/Projects/Specs";
const INTEGRATIONS = (pid: string) => `/projects/${pid}/integrations`;
const card = (page: Page, name: string) => page.getByRole("listitem").filter({ has: page.getByRole("heading", { name, exact: true }) });

const SCREENS = process.env.DROPBOX_SCREENS;
async function shot(page: Page, name: string): Promise<void> {
  if (SCREENS !== undefined && SCREENS !== "")
    await page.screenshot({ path: path.join(SCREENS, `${name}-${String(page.viewportSize()?.width)}.png`) });
}

/** Dropbox's consent page is never reached: the browser lands on a blank page, and the request is kept. */
function interceptDropbox(page: Page): string[] {
  const seen: string[] = [];
  allowOrigin(page, "https://www.dropbox.com");
  void page.route(
    (url) => url.origin === "https://www.dropbox.com",
    async (route) => {
      seen.push(route.request().url());
      await route.fulfill({ status: 200, contentType: "text/html", body: "<title>Dropbox</title><p>Dropbox sign-in (stub)</p>" });
    },
  );
  return seen;
}

test.afterEach(async () => {
  await seedDropbox({ reset: true });
  await seedIntegrations({ reset: true });
});

test.describe("Dropbox: the card, the Dropbox link form and the node", { tag: "@production" }, () => {
  test("the card: not set up asks to connect and says Graphico only reads; Connect goes to Dropbox's consent for offline access and comes back to the page", async ({
    page,
  }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Dropbox card") });
    await seedDropbox({ reset: true });
    const seen = interceptDropbox(page);
    await page.goto(INTEGRATIONS(id));
    await expect(card(page, "Dropbox")).toHaveAttribute(STATE, "off");
    await card(page, "Dropbox").getByRole("button", { name: "Set up" }).click();
    const set = dialog(page, "Dropbox");
    await expect(set.getByText("Dropbox asks only to view your files. Graphico only reads them.")).toBeVisible();
    await shot(page, "dropbox-card-disconnected");
    await set.getByRole("button", { name: "Connect your Dropbox account" }).click();
    await expect.poll(() => seen.length).toBeGreaterThan(0);
    const consent = new URL(seen[0] ?? "");
    expect(consent.searchParams.get("token_access_type")).toBe("offline");
    const state = (consent.searchParams.get("state") ?? "").split(".")[1] ?? "";
    expect((JSON.parse(Buffer.from(state, "base64url").toString()) as { r: string }).r).toBe(`${INTEGRATIONS(id)}?setup=dropbox`);
  });

  test("the return from Dropbox: the callback route posts the code, the account is bound and the browser goes back to the `next` page; the card then shows connected", async ({
    page,
    baseURL,
  }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Dropbox return") });
    await seedDropbox({ reset: true });
    allowOrigin(page, "https://www.dropbox.com");
    await page.route(
      (url) => url.origin === "https://www.dropbox.com",
      async (route) => {
        const state = new URL(route.request().url()).searchParams.get("state") ?? "";
        await route.fulfill({
          status: 302,
          headers: { location: `${baseURL ?? ""}/api/integrations/dropbox/callback?code=stub-code&state=${state}` },
        });
      },
    );
    const logged = (await stubLog()).length;
    const back = encodeURIComponent(`/projects/${id}`);
    await page.goto(`${INTEGRATIONS(id)}?setup=dropbox&next=${back}`);
    await dialog(page, "Dropbox").getByRole("button", { name: "Connect your Dropbox account" }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${id}$`)); // `next`: the page that asked
    const log = (await stubLog()).slice(logged);
    expect(log.filter((e) => e.method === "POST" && e.path === "/api/v1/integrations/dropbox/callback")).toHaveLength(1);
    expect(log.filter((e) => e.method === "PUT" && e.path === `/api/v1/projects/${id}/integrations/dropbox`)).toHaveLength(1);
    await page.goto(INTEGRATIONS(id));
    await expect(card(page, "Dropbox")).toHaveAttribute(STATE, "on");
    await expect(card(page, "Dropbox")).toContainText("Connected by dev");
  });

  test("a refused return says why in a toast and connects nothing: Dropbox's own denial and a code the API refuses", async ({ page }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 4\d\d/);
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Dropbox refused") });
    await seedDropbox({ reset: true });
    const payload = Buffer.from(JSON.stringify({ u: "u1", r: `${INTEGRATIONS(id)}?setup=dropbox`, p: "authorize", e: 1 })).toString(
      "base64url",
    );
    const state = ["n", payload, "stub-signature"].join(".");
    await page.goto(`/api/integrations/dropbox/callback?error=access_denied&state=${state}`);
    await expect(page.getByText("Dropbox was not connected: access was not allowed.")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${INTEGRATIONS(id)}\\?setup=dropbox$`));
    await expect(card(page, "Dropbox")).toHaveAttribute(STATE, "off");
    await seedDropbox({ callbackError: "dropbox_code_invalid" });
    await page.goto(`/api/integrations/dropbox/callback?code=stub-code&state=${state}`);
    await expect(page.getByText("Dropbox did not accept the sign-in. Try again.")).toBeVisible();
    await expect(card(page, "Dropbox")).toHaveAttribute(STATE, "off");
  });

  test("the card: connected shows who and which team (no Connect button) and the owner can disconnect; expired says Reconnect required and offers Reconnect Dropbox", async ({
    page,
  }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Dropbox states") });
    const seen = interceptDropbox(page);
    await seedIntegrations({ pid: id, dropbox: { by: ALEX, health: "active", workspace: ACME } });
    await page.goto(INTEGRATIONS(id));
    await expect(card(page, "Dropbox")).toHaveAttribute(STATE, "on");
    await expect(card(page, "Dropbox")).toContainText("Connected by Sam Ortiz");
    await card(page, "Dropbox").getByRole("button", { name: "Manage" }).click();
    const set = dialog(page, "Dropbox");
    await expect(set).toContainText("Acme Team · Connected by Sam Ortiz");
    await expect(set.getByRole("button", { name: /Connect your Dropbox account|Reconnect Dropbox/ })).toHaveCount(0);
    await expect(set.getByRole("button", { name: "Disconnect" })).toBeVisible();
    await shot(page, "dropbox-card-connected");
    await set.getByRole("button", { name: "Done" }).click();

    await seedDropbox({ connection: { status: "expired" } });
    await seedIntegrations({ pid: id, dropbox: { by: ALEX, health: "reconnect_required", workspace: ACME } });
    await page.goto(INTEGRATIONS(id));
    await expect(card(page, "Dropbox")).toHaveAttribute(STATE, "stopped");
    await expect(card(page, "Dropbox")).toContainText("Reconnect required");
    await card(page, "Dropbox").getByRole("button", { name: "Manage" }).click();
    await expect(set.getByRole("button", { name: "Connect your Dropbox account" })).toHaveCount(0);
    await shot(page, "dropbox-card-expired");
    await set.getByRole("button", { name: "Reconnect Dropbox" }).click();
    await expect.poll(() => seen.length).toBeGreaterThan(0); // an expired connection is not bound again: it goes to Dropbox
  });

  test("Dropbox link: no Dropbox on the project says Set up first and comes back with the form open; a link is checked (the closed codes in words), then added as a source that imports its files", async ({
    page,
    requests,
  }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of (4\d\d|502)/); // the refused checks are answers
    const pid = await canvas(page, unique("Dropbox form"));
    await page.getByRole("button", { name: ADD.source }).click();
    const plus = dialog(page, "Add a source");
    await expect(plus.getByRole("button", { name: /^Dropbox link/ })).toContainText(SET_UP_FIRST);
    await plus.getByRole("button", { name: /^Dropbox link/ }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/integrations\\?setup=dropbox&next=%2Fprojects%2F${pid}%3Fadd%3Ddropbox$`));
    await seedIntegrations({ pid, dropbox: { by: ALEX, health: "active", workspace: ACME } });
    await page.goto(`/projects/${pid}?add=dropbox`);
    const form = dialog(page, "Add a Dropbox link");
    await expect(form.getByRole("button", { name: CHECK_LINK })).toBeDisabled();
    const link = form.getByLabel("Dropbox link");
    for (const [text, words] of [
      ["https://evil.example.com/home/x", /not a link to a Dropbox folder or file, or it has a password/],
      ["https://www.dropbox.com/home", /not a link to a Dropbox folder or file, or it has a password/],
      ["https://www.dropbox.com/scl/fo/password/Shared", /or it has a password/],
      ["https://www.dropbox.com/home/Projects/404", /cannot open this folder or file/],
    ] as const) {
      await link.fill(text);
      await form.getByRole("button", { name: CHECK_LINK }).click();
      await expect(form.getByRole("alert")).toContainText(words);
      await expect(form.getByRole("button", { name: CHECK_LINK })).toBeVisible(); // still a check: nothing to add
      await expect(form.getByRole("group", { name: "Files to add" })).toHaveCount(0);
    }
    await shot(page, "dropbox-form-refused");
    for (const [code, words] of [
      ["dropbox_not_connected", /Connect Dropbox in Integrations first/],
      ["dropbox_reconnect_required", /needs a reconnect in Integrations/],
      ["dropbox_unreachable", /did not answer/],
    ] as const) {
      await page.route(`**/api/backend/projects/${pid}/sources/dropbox/check`, (route) =>
        route.fulfill({
          status: code === "dropbox_unreachable" ? 502 : 409,
          contentType: "application/json",
          body: JSON.stringify({ error: { code, message: code, details: {} } }),
        }),
      );
      await link.fill(FOLDER);
      await form.getByRole("button", { name: CHECK_LINK }).click();
      await expect(form.getByRole("alert")).toContainText(words);
      await page.unroute(`**/api/backend/projects/${pid}/sources/dropbox/check`);
    }
    await checkPickAndAdd(page, form, requests, pid, {
      app: "dropbox",
      name: "Dropbox",
      folder: FOLDER,
      big: "https://www.dropbox.com/home/Archive/big",
      exclude: ["id:file2", "id:file5"],
      href: /^https:\/\/www\.dropbox\.com\/home\//,
      shot,
    });
  });

  test("the node: importing counts files, done opens the Dropbox link, and no access and a failed import offer what is possible", async ({
    page,
  }) => {
    const pid = await canvas(page, unique("Dropbox node"));
    await seedIntegrations({ pid, dropbox: { by: ALEX, health: "active", workspace: ACME } });
    const files = (n: number, state: string) => Array.from({ length: n }, (_, i) => ({ name: `${state}-${String(i + 1)}.pdf`, state }));
    await seedSource(pid, {
      kind: "dropbox",
      name: "Project files",
      link: FOLDER,
      files: [...files(3, "done"), ...files(9, "processing")],
    });
    await page.goto(`/projects/${pid}`);
    const importing = nodes(page).filter({ hasText: "Project files" });
    await expect(importing).toHaveAttribute(STATE, "busy");
    await expect(importing).toContainText("3 of 12 files");
    await shot(page, "dropbox-node-importing");

    await seedSource(pid, { kind: "dropbox", name: "Spec.pdf", link: "https://www.dropbox.com/home/Spec.pdf", files: files(1, "done") });
    await seedSource(pid, {
      kind: "dropbox",
      name: "Old folder",
      link: "https://www.dropbox.com/home/Old",
      failure_reason: "no_access",
      files: [],
    });
    await seedSource(pid, {
      kind: "dropbox",
      name: "Broken folder",
      link: "https://www.dropbox.com/home/Broken",
      failure_reason: "unknown_error",
      files: [],
    });
    await page.goto(`/projects/${pid}`);
    const ready = nodes(page).filter({ hasText: "Spec.pdf" });
    await expect(ready).toHaveAttribute(STATE, "on");
    await expect(ready).toContainText("1 file · Dropbox");
    await shot(page, "dropbox-node-done");
    const stopped = nodes(page).filter({ hasText: "Old folder" });
    await expect(stopped).toHaveAttribute(STATE, "stopped");
    await stopped.getByRole("button").click();
    const detail = dialog(page, "Old folder");
    await expect(detail.getByText(/Dropbox needs a reconnect in Integrations/)).toBeVisible();
    await expect(detail.getByRole("link", { name: "Reconnect Dropbox" })).toHaveAttribute(
      "href",
      new RegExp(`/projects/${pid}/integrations\\?setup=dropbox`),
    );
    await shot(page, "dropbox-node-no-access");
    await page.keyboard.press("Escape");
    const broken = nodes(page).filter({ hasText: "Broken folder" });
    await expect(broken).toHaveAttribute(STATE, "stopped");
    await broken.getByRole("button").click();
    const failed = dialog(page, "Broken folder");
    await expect(failed.getByText(/Add the link again/)).toBeVisible();
    await expect(failed.getByRole("link", { name: "Reconnect Dropbox" })).toHaveCount(0);
    await shot(page, "dropbox-node-failed");
  });

  test("the header's Integrations link and the plus dialog agree: Dropbox is listed among the sources", async ({ page }) => {
    const pid = await canvas(page, unique("Dropbox plus"));
    await page.getByRole("button", { name: ADD.source }).click();
    await expect(dialog(page, "Add a source").getByRole("button", { name: /^Dropbox link/ })).toBeVisible();
    await shot(page, "dropbox-plus");
    await page.keyboard.press("Escape");
    await v2Header(page).getByRole("link", { name: "Integrations" }).click();
    await expect(page).toHaveURL(INTEGRATIONS(pid));
  });
});
