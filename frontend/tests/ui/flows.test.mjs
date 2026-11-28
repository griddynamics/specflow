// P17 s06 (FE-BP-03, FE-BP-09.AC1, D09): the UI tests of the production routes that stayed after the legacy screens went. UI behaviour
// (AC-6.5 OIDC sign-in, AC-6.7 logout, AC-12.2) driven with the already installed Playwright chromium against the BUILT server, the stub
// issuer + stub API (no backend, no model call). Ports 3213-3216. `/` is V2Home (D12): its first-run window, its project menu, and what
// it says when the projects cannot be read.
//
// The Mission Control upload/build coverage that lived here was deleted, not ported: the V2 path is already covered — the Upload form
// and its Content-Types, refusals and cap in upload-formats-production.spec.ts, the upload node and its files in
// feedback-1-production.spec.ts, the Build dialog (Medium the default, High posts {reasoning_level: 3}, Cancel posts nothing) in
// canvas-connectors-production.spec.ts, the whole build/gaps/plan journey in v2-journey-production.spec.ts, and the unknown-project
// gates of v2-production.spec.ts. The one assertion without a V2 home was the body of a build started on the DEFAULT reasoning level
// ({"reasoning_level":2}); canvas-connectors proves Medium is the default selection and the level numbering is the backend contract.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { chromium } from "@playwright/test";
import { frontendEnv, startFrontend } from "../support/servers.mjs";
import { startOidcStack } from "../support/oidc-stack.mjs";
import { oidcLogin } from "../support/session.mjs";

const FE_PORT = 3213;
const API_PORT = 3214;
const ISSUER_PORT = 3216;
const EMAIL = "ui.user@example.test";
let stack, api, fe, browser, jar;

before(async () => {
  stack = await startOidcStack({ issuerPort: ISSUER_PORT, apiPort: API_PORT, defaultEmail: EMAIL });
  api = stack.api;
  fe = await startFrontend({ port: FE_PORT, env: frontendEnv(FE_PORT, { APP_ENV: "local", ...stack.env }) });
  jar = await oidcLogin(fe.base, EMAIL);
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
  await fe?.stop();
  await stack?.close();
});

async function newPage({ loggedIn = true } = {}) {
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
  return { context, page, errors };
}

test("AC-6.5, AC-6.7, FE-BP-03.AC2, AC4, AC5: OIDC sign-in button, the first-run window of `/` creates a project, sign out ends the session", async () => {
  const { context, page, errors } = await newPage({ loggedIn: false });
  try {
    await page.goto(fe.base + "/");
    await page.waitForURL(fe.base + "/"); // signed out: the proxy sends it to /login, which signs in at once (stub issuer)
    // No project yet (first test on a fresh stub): the first-run window offers "Create project" (O12, D12).
    const first = page.getByRole("dialog", { name: "Create your first project" });
    await first.waitFor();
    const name = `UI project ${Date.now()}`;
    await first.getByLabel("Project name").fill(name);
    await first.getByRole("button", { name: "Create project" }).click();
    await page.waitForURL(/\/projects\/[0-9a-f-]{36}$/);
    await page.getByRole("button", { name: "Projects" }).filter({ hasText: name }).waitFor(); // the menu shows the new project
    const created = api.log.filter((e) => e.method === "POST" && e.path === "/api/v1/projects");
    assert.equal(created.length, 1, "one POST /projects (name only, A9)");
    // Sign out from the avatar menu (no "Reset demo" in production). Wait for quiet first: a BFF response still in flight after the
    // sign-out could write the session cookie again (GAPS G-P17-4).
    await page.waitForLoadState("networkidle");
    await page.getByRole("button", { name: "Account" }).click();
    assert.equal(await page.getByRole("menuitem", { name: "Reset demo" }).count(), 0);
    const logout = page.waitForRequest((r) => r.url().includes("/protocol/openid-connect/logout"), { timeout: 15_000 });
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    const back = new URL(new URL((await logout).url()).searchParams.get("post_logout_redirect_uri") ?? "");
    assert.equal(back.pathname + back.search, "/login?signedout=idp", "the provider returns to /login, which signs in again at once");
    // The stub issuer keeps no session of its own, so that sign-in lands on `/` and the newest canvas again; the app session was replaced.
    await page.getByRole("button", { name: "Account" }).waitFor();
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
    const msg = page.getByRole("alert").filter({ hasText: "could not be loaded" });
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
