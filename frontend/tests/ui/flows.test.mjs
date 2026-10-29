// P17 s06 (FE-BP-09.AC1, D09): the hello UI tests ported to the new screens on the production routes. UI behaviour (AC-12.2,
// AC-12.3, AC-6.5 login form, AC-6.7 logout; FE-BP-03, FE-BP-08) driven with the already installed Playwright chromium against the
// BUILT server + stub API (no backend, no model call). Ports 3213-3214. Time-dependent checks use Playwright's fake clock.
// Mapping (TECH-SPECS §1.7): `/` project list + create form -> `/` (newest project or the empty state) + the "New" modal;
// `/projects/<id>` upload -> Mission Control, now the sources screen (GDRIVE plan 07): Local files card, parse on the server.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { chromium } from "@playwright/test";
import { frontendEnv, randomSecret, startFrontend } from "../support/servers.mjs";
import { startStubApi } from "../support/stub-api.mjs";
import { devLogin } from "../support/session.mjs";
import { syntheticPdf } from "../support/pdf.mjs";

const FE_PORT = 3213;
const API_PORT = 3214;
const KEY = randomSecret();
const EMAIL = "ui.user@example.test";
const TIMING = { parseMs: 1500, summarizeMs: 1500, parseOutcome: "parsed" };
let api, fe, browser, jar;

before(async () => {
  api = await startStubApi({ port: API_PORT, signingKey: KEY, ...TIMING });
  fe = await startFrontend({
    port: FE_PORT,
    env: frontendEnv(FE_PORT, {
      APP_ENV: "local",
      AUTH_MODE: "dev",
      API_INTERNAL_URL: api.url,
      SESSION_SECRET: randomSecret(),
      AUTH_DEV_SIGNING_KEY: KEY,
      LOCAL_TEST_USER_EMAIL: EMAIL,
    }),
  });
  jar = await devLogin(fe.base, EMAIL);
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
  await fe?.stop();
  await api?.close();
});

async function newPage({ loggedIn = true, clock = false } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  if (loggedIn) {
    const cookies = jar
      .header()
      .split("; ")
      .map((c) => {
        const i = c.indexOf("=");
        return { name: c.slice(0, i), value: c.slice(i + 1), url: fe.base };
      });
    await context.addCookies(cookies);
  }
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  if (clock) await page.clock.install();
  return { context, page, errors };
}

const bff = (path, init = {}) =>
  fetch(`${fe.base}/api/backend${path}`, {
    ...init,
    headers: { cookie: jar.header(), origin: fe.base, "x-requested-with": "specflow", "content-type": "application/json", ...init.headers },
  });

async function seedProject(name) {
  return (await (await bff("/projects", { method: "POST", body: JSON.stringify({ name }) })).json()).id;
}
const mc = (pid) => `${fe.base}/projects/${pid}/mission-control`;
const fileInput = (page) => page.getByTestId("mcs-upload-input");
const calls = (pid, action) =>
  api.log.filter((e) => e.method === "POST" && e.path.startsWith(`/api/v1/projects/${pid}/documents/`) && e.path.endsWith(`/${action}`))
    .length;
// The project's source cards; the tests below add Local files only, and they check that the card is the upload kind.
const card = (page) => page.getByTestId("source-card");
// The card once the server parsed its files and the poll read it: its state text (from the same state as `data-state`) is "Done".
const doneCard = (page) => card(page).filter({ has: page.getByTestId("source-state-text").filter({ hasText: /^Done$/ }) });

test("AC-6.5, AC-6.7, FE-BP-03.AC2, AC4, AC5: dev login form, the empty `/` creates a project through New, sign out ends the session", async () => {
  const { context, page, errors } = await newPage({ loggedIn: false });
  try {
    await page.goto(fe.base + "/");
    await page.waitForURL("**/login");
    assert.equal(await page.getByTestId("login-dev-email").inputValue(), EMAIL);
    await page.getByTestId("login-dev-submit").click();
    await page.waitForURL(fe.base + "/");
    // No project yet (first test on a fresh stub): the empty state offers "New project" (O12).
    await page.getByTestId("home-empty").waitFor();
    await page.getByTestId("home-new").click();
    const name = `UI project ${Date.now()}`;
    await page.getByLabel("Name").fill(name);
    await page.getByTestId("cp-create").click();
    await page.waitForURL(/\/projects\/[0-9a-f-]{36}\/mission-control$/);
    await page.getByTestId("mcs-empty").waitFor();
    assert.equal(await page.getByTestId("topbar-project-name").textContent(), name, "the switcher shows the new project");
    const created = api.log.filter((e) => e.method === "POST" && e.path === "/api/v1/projects");
    assert.equal(created.length, 1, "one POST /projects (name only, A9)");
    // Sign out from the avatar menu (no "Reset demo" in production). Wait for quiet first: a BFF response still in flight after the
    // sign-out could write the session cookie again (GAPS G-P17-4).
    await page.waitForLoadState("networkidle");
    await page.getByRole("button", { name: /^Account/ }).click();
    assert.equal(await page.getByRole("menuitem", { name: "Reset demo" }).count(), 0);
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    await page.waitForURL(/\/login\?signedout=1$/, { timeout: 15_000 });
    await page.getByTestId("login-page").waitFor();
    assert.equal(await page.getByTestId("login-signed-out").count(), 0, "D21: no signed-out note");
    assert.equal(await page.getByTestId("login-still-signed-in").count(), 0, "the session is gone, so no still-signed-in alert");
    await page.goto(fe.base + "/");
    await page.waitForURL("**/login"); // the session is gone
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
});

test("AC-12.2, K4, K5: Local files through the sources screen send the raw PDF, show the Local files card, the server parses (no browser parse), and the Build button unlocks with 'not available' (Q12)", async () => {
  const pid = await seedProject(`Upload ${Date.now()}`);
  const { context, page, errors } = await newPage();
  try {
    const uploads = [];
    page.on("request", (r) => {
      if (r.method() === "POST" && r.url().includes("/files?filename=")) uploads.push(r);
    });
    await page.goto(mc(pid));
    await page.getByTestId("mcs-empty").waitFor();
    assert.equal(await card(page).count(), 0);
    const pdf = syntheticPdf(4096);
    await fileInput(page).setInputFiles({ name: "spec one.pdf", mimeType: "application/pdf", buffer: pdf });
    await card(page).waitFor();
    assert.equal(await card(page).getAttribute("data-kind"), "upload");
    await doneCard(page).waitFor({ timeout: 20_000 }); // parsed by the server, read by the poll
    assert.equal(uploads.length, 1);
    const headers = await uploads[0].allHeaders();
    assert.equal(headers["content-type"], "application/pdf");
    assert.equal(headers["x-requested-with"], "specflow");
    assert.ok(uploads[0].url().includes("filename=spec%20one.pdf"));
    const stored = api.log.filter((e) => e.path.includes("/files?filename=")).at(-1);
    assert.equal(stored.bytes, pdf.length);
    assert.equal(stored.head, "%PDF-", "raw body: the first bytes are the PDF, not a multipart boundary");
    assert.equal(calls(pid, "parse"), 0, "the browser never calls parse (K5)");
    assert.equal(calls(pid, "summarize"), 0, "the browser never calls summarize (K1, K5)");
    assert.equal(await page.getByTestId("screen-mission-control").getAttribute("data-phase"), "ready_to_build");
    // Q12: unlocked, a click shows the existing "not available" message and sends nothing.
    const before = api.log.length;
    await page.getByTestId("kb-build").click();
    await page.getByText("Building knowledge is not available yet.").waitFor();
    assert.equal(await page.getByText(/\bdemo\b/i).count(), 0, "production never says demo");
    assert.equal(api.log.length, before, "the click made no request");
    // K3: GitHub is not offered in Add source; Google Drive and Local files are.
    await page.getByTestId("mcs-add-source").click();
    await page.getByRole("menuitem", { name: /Google Drive/ }).waitFor();
    assert.equal(await page.getByRole("menuitem", { name: /GitHub/ }).count(), 0);
    assert.equal(await page.getByRole("menuitem", { name: /Local files/ }).count(), 1);
    assert.equal(await page.getByText("Upload PDF files").count(), 1);
    assert.equal(await fileInput(page).getAttribute("accept"), "application/pdf,.pdf");
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
});

test("AC-7.6, AC-7.7: an upload of bytes already in the project adds no second document; a non-PDF is not sent (the PDF-only toast names it) and the good file stays", async () => {
  const pdf = syntheticPdf(300);
  const pid = await seedProject(`Dup and non-PDF ${Date.now()}`);
  const { context, page, errors } = await newPage();
  try {
    await page.goto(mc(pid));
    await page.getByTestId("mcs-empty").waitFor();
    await fileInput(page).setInputFiles([
      { name: "same.pdf", mimeType: "application/pdf", buffer: pdf },
      { name: "same copy.pdf", mimeType: "application/pdf", buffer: pdf },
      { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("not a pdf") },
    ]);
    await page.getByText("notes.txt was not added; Specflow reads PDF files.").waitFor({ timeout: 15_000 });
    // The success toast does not call the duplicate "added".
    await page.getByText("1 file added", { exact: true }).waitFor({ timeout: 15_000 });
    await page.getByText("1 file is already in this project; nothing new was added.").waitFor({ timeout: 15_000 });
    const uploadsOf = (p) =>
      api.log.filter(
        (e) => e.method === "POST" && e.path.startsWith(`/api/v1/projects/${p}/sources/`) && e.path.includes("/files?filename="),
      );
    for (const end = Date.now() + 5000; uploadsOf(pid).length < 2 && Date.now() < end;) await new Promise((r) => setTimeout(r, 50));
    assert.equal(uploadsOf(pid).length, 2, "the PDFs went to the API, one request each; the text file never did");
    assert.equal(uploadsOf(pid).at(-1).contentType, "application/pdf");
    const docs = (await (await bff(`/projects/${pid}/documents`)).json()).items;
    assert.equal(docs.length, 1, "the duplicate and the rejected file add no document");
    await doneCard(page).waitFor({ timeout: 20_000 }); // parsed by the server, read by the poll
    assert.equal(await card(page).getAttribute("data-kind"), "upload");
    assert.equal(await card(page).getByTestId("source-counts").textContent(), "1 file"); // FR-SRC-0003
    assert.equal(calls(pid, "parse"), 0);
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
});

test("FE-BP-03.AC10, AC7: an unknown or non-UUID project id and the old hello routes lead to Mission Control or `/`", async () => {
  const pid = await seedProject(`Newest ${Date.now()}`);
  const { context, page, errors } = await newPage();
  try {
    for (const bad of ["00000000-0000-0000-0000-000000000000", "not-a-uuid"]) {
      await page.goto(`${fe.base}/projects/${bad}/decisions`);
      await page.waitForURL(mc(pid), { timeout: 15_000 }); // -> `/` -> the newest project's Mission Control (O12)
    }
    for (const old of [`/projects/${pid}`, `/projects/${pid}/documents/00000000-0000-0000-0000-000000000001`]) {
      const res = await fetch(fe.base + old, { redirect: "manual", headers: { cookie: jar.header() } });
      assert.equal(res.status, 307, old);
      assert.equal(new URL(res.headers.get("location"), fe.base).pathname, `/projects/${pid}/mission-control`, old);
    }
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
});

test("a non-JSON error body (HTML 502 from an ingress) shows a generic message, never raw HTML", async () => {
  const { context, page, errors } = await newPage();
  try {
    await page.route("**/api/backend/projects?*", (route) =>
      route.fulfill({ status: 502, contentType: "text/html", body: "<html><body><h1>Bad Gateway SECRET-HTML</h1></body></html>" }),
    );
    await page.goto(fe.base + "/");
    const msg = page.getByTestId("home-error");
    await msg.waitFor();
    const text = await msg.textContent();
    assert.match(text, /could not be loaded/);
    assert.doesNotMatch(text, /Bad Gateway|SECRET-HTML|<html|SyntaxError|JSON/);
    assert.doesNotMatch(await page.content(), /SECRET-HTML/);
    assert.deepEqual(errors, [], "the page did not crash");
  } finally {
    await context.close();
  }
});
