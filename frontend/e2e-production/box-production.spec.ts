// Box on the PRODUCTION routes in a real browser, against the stub API through the real BFF (docs/box/oauth.md, docs/box/source.md): the Box card of the
// Integrations page (not set up, connected, expired), Connect going to Box's consent, and the canvas "Box link" form (Set up first, the check with each
// closed refusal in words, Add source) and its node (importing, done, stopped). Box's own pages are intercepted and never reached. Screenshots of the
// states go to the folder in `BOX_SCREENS` when it is set (never inside the repository).
import path from "node:path";
import type { Page } from "@playwright/test";
import { ADD, bodiesOf, canvas, dialog, nodes, STATE, unique } from "./canvas-support";
import {
  allowConsoleErrors,
  allowOrigin,
  expect,
  loginV2,
  seedBox,
  seedIntegrations,
  seedMembers,
  seedSource,
  stubLog,
  test,
  v2Header,
} from "./support";

const ALEX = { user_id: "someone", name: "Sam Ortiz" };
const ACME = { id: "enterprise-stub", name: "Acme Corp" };
const SET_UP_FIRST = "Set up first";
const CHECK_LINK = "Check link";
const FOLDER = "https://app.box.com/folder/12345";
const INTEGRATIONS = (pid: string) => `/projects/${pid}/integrations`;
const card = (page: Page, name: string) => page.getByRole("listitem").filter({ has: page.getByRole("heading", { name, exact: true }) });

const SCREENS = process.env.BOX_SCREENS;
async function shot(page: Page, name: string): Promise<void> {
  if (SCREENS !== undefined && SCREENS !== "")
    await page.screenshot({ path: path.join(SCREENS, `${name}-${String(page.viewportSize()?.width)}.png`) });
}

/** Box's consent page is never reached: the browser lands on a blank page, and the request is kept. */
function interceptBox(page: Page): string[] {
  const seen: string[] = [];
  allowOrigin(page, "https://account.box.com");
  void page.route(
    (url) => url.origin === "https://account.box.com",
    async (route) => {
      seen.push(route.request().url());
      await route.fulfill({ status: 200, contentType: "text/html", body: "<title>Box</title><p>Box sign-in (stub)</p>" });
    },
  );
  return seen;
}

test.afterEach(async () => {
  await seedBox({ reset: true });
  await seedIntegrations({ reset: true });
});

test.describe("Box: the card, the Box link form and the node", { tag: "@production" }, () => {
  test("the card: not set up asks to connect and says Graphico only reads; Connect goes to Box's consent for the one scope and comes back to the page", async ({
    page,
  }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Box card") });
    await seedBox({ reset: true });
    const seen = interceptBox(page);
    await page.goto(INTEGRATIONS(id));
    await expect(card(page, "Box")).toHaveAttribute(STATE, "off");
    await card(page, "Box").getByRole("button", { name: "Set up" }).click();
    const set = dialog(page, "Box");
    await expect(set.getByText('Box asks for "Read and write". Graphico only reads your files.')).toBeVisible();
    await expect(set.getByText(/Read and write/)).toHaveCount(1); // said once
    await shot(page, "box-card-disconnected");
    await set.getByRole("button", { name: "Connect your Box account" }).click();
    await expect.poll(() => seen.length).toBeGreaterThan(0);
    const consent = new URL(defined(seen[0]));
    expect(consent.searchParams.get("scope")).toBe("root_readwrite");
    const state = (consent.searchParams.get("state") ?? "").split(".")[1] ?? "";
    expect((JSON.parse(Buffer.from(state, "base64url").toString()) as { r: string }).r).toBe(`${INTEGRATIONS(id)}?setup=box`);
  });

  test("the return from Box: the callback route posts the code, the account is bound and the browser goes back to the `next` page; the card then shows connected", async ({
    page,
    baseURL,
  }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Box return") });
    await seedBox({ reset: true });
    allowOrigin(page, "https://account.box.com");
    await page.route(
      (url) => url.origin === "https://account.box.com",
      async (route) => {
        const state = new URL(route.request().url()).searchParams.get("state") ?? "";
        await route.fulfill({
          status: 302,
          headers: { location: `${baseURL ?? ""}/api/integrations/box/callback?code=stub-code&state=${state}` },
        });
      },
    );
    const logged = (await stubLog()).length;
    const back = encodeURIComponent(`/projects/${id}`);
    await page.goto(`${INTEGRATIONS(id)}?setup=box&next=${back}`);
    await dialog(page, "Box").getByRole("button", { name: "Connect your Box account" }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${id}$`)); // `next`: the page that asked
    const log = (await stubLog()).slice(logged);
    expect(log.filter((e) => e.method === "POST" && e.path === "/api/v1/integrations/box/callback")).toHaveLength(1);
    expect(log.filter((e) => e.method === "PUT" && e.path === `/api/v1/projects/${id}/integrations/box`)).toHaveLength(1);
    await page.goto(INTEGRATIONS(id));
    await expect(card(page, "Box")).toHaveAttribute(STATE, "on");
    await expect(card(page, "Box")).toContainText("Connected by dev");
    await card(page, "Box").getByRole("button", { name: "Manage" }).click();
    await expect(dialog(page, "Box")).toContainText("Acme Corp · Connected by dev");
  });

  test("a refused return says why in a toast and connects nothing: Box's own denial and a code the API refuses", async ({ page }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 4\d\d/);
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Box refused") });
    await seedBox({ reset: true });
    const payload = Buffer.from(JSON.stringify({ u: "u1", r: `${INTEGRATIONS(id)}?setup=box`, p: "authorize", e: 1 })).toString(
      "base64url",
    );
    const state = ["n", payload, "stub-signature"].join(".");
    await page.goto(`/api/integrations/box/callback?error=access_denied&state=${state}`);
    await expect(page.getByText("Box was not connected: access was not allowed.")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${INTEGRATIONS(id)}\\?setup=box$`));
    await expect(card(page, "Box")).toHaveAttribute(STATE, "off");
    await seedBox({ callbackError: "box_code_invalid" });
    await page.goto(`/api/integrations/box/callback?code=stub-code&state=${state}`);
    await expect(page.getByText("Box did not accept the sign-in. Try again.")).toBeVisible();
    await expect(card(page, "Box")).toHaveAttribute(STATE, "off");
  });

  test("the card: connected shows who and which Box account (no Connect button) and the owner can disconnect; expired says Reconnect required and offers Reconnect Box", async ({
    page,
  }) => {
    await loginV2(page);
    const { id } = await seedMembers({ name: unique("Box states") });
    const seen = interceptBox(page);
    await seedIntegrations({ pid: id, box: { by: ALEX, health: "active", workspace: ACME } });
    await page.goto(INTEGRATIONS(id));
    await expect(card(page, "Box")).toHaveAttribute(STATE, "on");
    await expect(card(page, "Box")).toContainText("Connected by Sam Ortiz");
    await card(page, "Box").getByRole("button", { name: "Manage" }).click();
    const set = dialog(page, "Box");
    await expect(set).toContainText("Acme Corp · Connected by Sam Ortiz");
    await expect(set.getByRole("button", { name: /Connect your Box account|Reconnect Box/ })).toHaveCount(0);
    await expect(set.getByRole("button", { name: "Disconnect" })).toBeVisible();
    await shot(page, "box-card-connected");
    await set.getByRole("button", { name: "Done" }).click();

    await seedBox({ connection: { status: "expired" } });
    await seedIntegrations({ pid: id, box: { by: ALEX, health: "reconnect_required", workspace: ACME } });
    await page.goto(INTEGRATIONS(id));
    await expect(card(page, "Box")).toHaveAttribute(STATE, "stopped");
    await expect(card(page, "Box")).toContainText("Reconnect required");
    await card(page, "Box").getByRole("button", { name: "Manage" }).click();
    await expect(set.getByRole("button", { name: "Connect your Box account" })).toHaveCount(0);
    await shot(page, "box-card-expired");
    await set.getByRole("button", { name: "Reconnect Box" }).click();
    await expect.poll(() => seen.length).toBeGreaterThan(0); // an expired connection is not bound again: it goes to Box
  });

  test("Box link: no Box on the project says Set up first and comes back with the form open; a link is checked (the closed codes in words), then added as a source that imports its files", async ({
    page,
    requests,
  }) => {
    allowConsoleErrors(page, /Failed to load resource: the server responded with a status of 4\d\d/); // the refused checks are answers
    const pid = await canvas(page, unique("Box form"));
    await page.getByRole("button", { name: ADD.source }).click();
    const plus = dialog(page, "Add a source");
    await expect(plus.getByRole("button", { name: /^Box link/ })).toContainText(SET_UP_FIRST);
    await plus.getByRole("button", { name: /^Box link/ }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${pid}/integrations\\?setup=box&next=%2Fprojects%2F${pid}%3Fadd%3Dbox$`));
    await seedIntegrations({ pid, box: { by: ALEX, health: "active", workspace: ACME } });
    await page.goto(`/projects/${pid}?add=box`);
    const form = dialog(page, "Add a Box link");
    await expect(form.getByRole("button", { name: CHECK_LINK })).toBeDisabled();
    const link = form.getByLabel("Box link");
    for (const [text, words] of [
      ["https://evil.example.com/folder/1", /not a link to a Box folder, file or shared link/],
      ["https://app.box.com/folder/0", /not a link to a Box folder, file or shared link/],
      ["https://app.box.com/s/password", /has a password/],
      ["https://app.box.com/folder/404", /cannot open this folder or file/],
    ] as const) {
      await link.fill(text);
      await form.getByRole("button", { name: CHECK_LINK }).click();
      await expect(form.getByRole("alert")).toContainText(words);
      await expect(form.getByRole("button", { name: CHECK_LINK })).toBeVisible(); // still a check: nothing to add
      await expect(form.getByRole("group", { name: "Files to add" })).toHaveCount(0);
    }
    await shot(page, "box-form-refused");
    for (const [code, words] of [
      ["box_not_connected", /Connect Box in Integrations first/],
      ["box_reconnect_required", /needs a reconnect in Integrations/],
    ] as const) {
      await page.route(`**/api/backend/projects/${pid}/sources/box/check`, (route) =>
        route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ error: { code, message: code, details: {} } }),
        }),
      );
      await link.fill(FOLDER);
      await form.getByRole("button", { name: CHECK_LINK }).click();
      await expect(form.getByRole("alert")).toContainText(words);
      await page.unroute(`**/api/backend/projects/${pid}/sources/box/check`);
    }
    await link.fill(FOLDER);
    await form.getByRole("button", { name: CHECK_LINK }).click();
    await expect(form.getByText("Folder Project files · 12 files (1.2 MB), 3 skipped (other types), 2 folders.")).toBeVisible();
    await expect(form.getByRole("alert")).toHaveCount(0);
    const list = form.getByRole("group", { name: "Files to add" });
    await expect(list.getByRole("checkbox")).toHaveCount(12);
    await expect(list.getByRole("checkbox", { checked: true })).toHaveCount(12);
    await shot(page, "box-form-checked");
    await link.fill(`${FOLDER}/`); // a different link is a new check
    await expect(form.getByRole("button", { name: CHECK_LINK })).toBeVisible();
    await link.fill("https://app.box.com/folder/9999");
    await form.getByRole("button", { name: CHECK_LINK }).click();
    await expect(form.getByText(/Folder Big archive · 1000 files \(.*\), 20 skipped \(other types\), 300 folders\./)).toBeVisible();
    await expect(form.getByText(/more files than one import takes/)).toBeVisible();
    await shot(page, "box-form-capped");
    await link.fill(FOLDER);
    await form.getByRole("button", { name: CHECK_LINK }).click();
    await expect(form.getByText(/^Folder Project files · 12 files/)).toBeVisible();
    await list.getByRole("checkbox", { name: /File 2\.docx/ }).uncheck();
    await list.getByRole("checkbox", { name: /File 5\.html/ }).uncheck();
    await expect(form.getByText("10 of 12 files")).toBeVisible();
    await shot(page, "box-form-unchecked");
    await form.getByRole("button", { name: "Select none" }).click();
    await expect(form.getByText("No files")).toBeVisible();
    await expect(form.getByRole("button", { name: "Add source" })).toBeDisabled(); // nothing to add
    await form.getByRole("button", { name: "Select all" }).click();
    await list.getByRole("checkbox", { name: /File 2\.docx/ }).uncheck();
    await list.getByRole("checkbox", { name: /File 5\.html/ }).uncheck();
    await form.getByRole("button", { name: "Add source" }).click();
    await expect(form).toBeHidden();
    const node = nodes(page).filter({ hasText: "Project files" });
    await expect(node).toHaveAttribute(STATE, "busy");
    await expect(node).toHaveAttribute(STATE, "on", { timeout: 15_000 });
    await expect(node).toContainText("13 files · Box"); // 12 listed - 2 left out + 3 skipped
    const adds = requests.filter((r) => r.method === "POST" && r.path === `/api/backend/projects/${pid}/sources/box`);
    expect(bodiesOf(adds)).toEqual([{ link: FOLDER, exclude: ["1002", "1005"] }]);
    await node.getByRole("button").click();
    const detail = dialog(page, "Project files");
    await expect(detail.getByText(/^13 files\. Imported when it was added/)).toBeVisible();
    await expect(detail.getByRole("link", { name: "Open in Box" })).toHaveAttribute("href", FOLDER);
    await expect(detail.getByRole("button", { name: "Remove" })).toBeVisible();
  });

  test("the node: importing counts files, done opens the Box link, and no access offers Reconnect Box", async ({ page }) => {
    const pid = await canvas(page, unique("Box node"));
    await seedIntegrations({ pid, box: { by: ALEX, health: "active", workspace: ACME } });
    const files = (n: number, state: string) => Array.from({ length: n }, (_, i) => ({ name: `${state}-${String(i + 1)}.pdf`, state }));
    await seedSource(pid, {
      kind: "box",
      name: "Project files",
      link: FOLDER,
      files: [...files(3, "done"), ...files(9, "processing")],
    });
    await page.goto(`/projects/${pid}`);
    const importing = nodes(page).filter({ hasText: "Project files" });
    await expect(importing).toHaveAttribute(STATE, "busy");
    await expect(importing).toContainText("3 of 12 files");
    await shot(page, "box-node-importing");

    await seedSource(pid, { kind: "box", name: "Spec.pdf", link: "https://app.box.com/file/77", files: files(1, "done") });
    await seedSource(pid, {
      kind: "box",
      name: "Old folder",
      link: "https://app.box.com/folder/404",
      failure_reason: "no_access",
      files: [],
    });
    await page.goto(`/projects/${pid}`);
    const ready = nodes(page).filter({ hasText: "Spec.pdf" });
    await expect(ready).toHaveAttribute(STATE, "on");
    await expect(ready).toContainText("1 file · Box");
    await shot(page, "box-node-done");
    const stopped = nodes(page).filter({ hasText: "Old folder" });
    await expect(stopped).toHaveAttribute(STATE, "stopped");
    await stopped.getByRole("button").click();
    const detail = dialog(page, "Old folder");
    await expect(detail.getByText(/Another member reconnects Box/)).toBeVisible();
    await expect(detail.getByRole("link", { name: "Reconnect Box" })).toHaveAttribute(
      "href",
      new RegExp(`/projects/${pid}/integrations\\?setup=box`),
    );
    await shot(page, "box-node-no-access");
  });

  test("the header's Integrations link and the plus dialog agree: Box is listed among the sources", async ({ page }) => {
    const pid = await canvas(page, unique("Box plus"));
    await page.getByRole("button", { name: ADD.source }).click();
    await expect(dialog(page, "Add a source").getByRole("button", { name: /^Box link/ })).toBeVisible();
    await shot(page, "box-plus");
    await page.keyboard.press("Escape");
    await v2Header(page).getByRole("link", { name: "Integrations" }).click();
    await expect(page).toHaveURL(INTEGRATIONS(pid));
  });
});

function defined<T>(v: T | undefined): T {
  if (v === undefined) throw new Error("expected a value");
  return v;
}
