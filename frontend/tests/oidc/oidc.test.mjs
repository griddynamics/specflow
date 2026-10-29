// AC-16.8 / AC-6.9: OIDC contract test. Local Node issuer + the built server in
// AUTH_MODE=oidc with OIDC_INTERNAL_URL (the public issuer host does not resolve, so
// every back-channel call must go through the internal URL). Ports 3220-3229.
// Covers AC-6.5 (credentials callback yields no session), AC-6.8 (single-flight refresh,
// failed refresh clears the session), AC-6.9 (login -> session, refresh, logout).
import { request } from "node:http";
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { frontendEnv, randomSecret, startFrontend } from "../support/servers.mjs";
import { startStubApi } from "../support/stub-api.mjs";
import { Jar, csrf, sessionOf } from "../support/session.mjs";
import { startStubIssuer } from "./stub-issuer.mjs";

const FE_PORT = 3220;
const API_PORT = 3221;
const ISSUER_PORT = 3225;
const PUBLIC_ISSUER = "http://issuer.example.invalid/realms/stub"; // .invalid never resolves
const CLIENT_ID = "specflow-web";
const CLIENT_SECRET = randomSecret();
let issuer, api, fe;

before(async () => {
  issuer = await startStubIssuer({ port: ISSUER_PORT, publicIssuer: PUBLIC_ISSUER, clientId: CLIENT_ID, clientSecret: CLIENT_SECRET });
  api = await startStubApi({ port: API_PORT });
  fe = await startFrontend({
    port: FE_PORT,
    env: frontendEnv(FE_PORT, {
      APP_ENV: "local",
      AUTH_MODE: "oidc",
      API_INTERNAL_URL: api.url,
      SESSION_SECRET: randomSecret(),
      OIDC_ISSUER: PUBLIC_ISSUER,
      OIDC_INTERNAL_URL: issuer.url,
      OIDC_CLIENT_ID: CLIENT_ID,
      OIDC_CLIENT_SECRET: CLIENT_SECRET,
    }),
  });
});

after(async () => {
  await fe?.stop();
  await api?.close();
  await issuer?.close();
});

const bearerSeenByApi = () => api.log.at(-1).headers.authorization;

/** Sign in the way a browser does; `expiresIn` shapes the first access token. */
async function login({ expiresIn = 3600, email = "oidc.user@example.test" } = {}) {
  issuer.accessExpiresIn = expiresIn;
  const jar = new Jar();
  const token = await csrf(fe.base, jar);
  const start = await fetch(`${fe.base}/api/auth/signin/oidc`, {
    method: "POST",
    redirect: "manual",
    headers: { cookie: jar.header(), "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ csrfToken: token, callbackUrl: `${fe.base}/` }),
  });
  jar.absorb(start);
  assert.equal(start.status, 302);
  const authorize = new URL(start.headers.get("location"));
  authorize.searchParams.set("login_hint", email); // the stub issuer signs this user in
  // The "browser" cannot resolve the public host: talk to the stub directly.
  const approved = await fetch(
    `http://127.0.0.1:${ISSUER_PORT}${authorize.pathname.replace("/realms/stub", "/realms/stub")}${authorize.search}`,
    { redirect: "manual" },
  );
  assert.equal(approved.status, 302);
  const callback = await fetch(approved.headers.get("location"), { redirect: "manual", headers: { cookie: jar.header() } });
  jar.absorb(callback);
  assert.equal(callback.status, 302, "callback redirects after a successful login");
  return { jar, authorize };
}

test("AC-6.5: the login page shows the OIDC button and no dev form", async () => {
  const html = await (await fetch(fe.base + "/login")).text();
  assert.match(html, /data-testid="login-oidc"/);
  assert.match(html, /Sign in with Google/);
  assert.doesNotMatch(html, /login-dev-email|login-dev-submit/);
});

test("AC-6.5/6.9: sign-in redirects to the OIDC_ISSUER authorize endpoint with PKCE, state and nonce", async () => {
  const { authorize } = await login();
  assert.equal(
    authorize.origin + authorize.pathname,
    `${PUBLIC_ISSUER.replace("/realms/stub", "")}/realms/stub/protocol/openid-connect/auth`,
  );
  const q = issuer.authorizeRequests.at(-1);
  assert.equal(q.client_id, CLIENT_ID);
  assert.equal(q.code_challenge_method, "S256");
  assert.ok(q.code_challenge && q.state && q.nonce);
  assert.match(q.scope, /openid/);
  assert.equal(q.redirect_uri, `${fe.base}/api/auth/callback/oidc`, "redirect_uri is pinned to PUBLIC_URL");
  const exchange = issuer.tokenRequests.find((r) => r.grant_type === "authorization_code");
  assert.equal(exchange.ok, true, "the code exchange passed PKCE verification via the internal URL");
});

test("AC-6.9: login -> session (name and email only); the BFF forwards the issuer's access token", async () => {
  const { jar } = await login();
  const session = await sessionOf(fe.base, jar);
  assert.equal(session.user.email, "oidc.user@example.test");
  assert.deepEqual(Object.keys(session.user).sort(), ["email", "name"]);
  assert.doesNotMatch(JSON.stringify(session), /at-\d|rt-\d|eyJ/);
  assert.ok(jar.names().includes("specflow-session") || jar.names().some((n) => n.startsWith("specflow-session.")));

  const res = await fetch(`${fe.base}/api/backend/me`, { headers: { cookie: jar.header() } });
  assert.equal(res.status, 200);
  assert.match(bearerSeenByApi(), /^Bearer at-\d+$/);
  const page = await fetch(fe.base + "/", { headers: { cookie: jar.header() }, redirect: "manual" });
  assert.equal(page.status, 200, "pages render for a signed-in user");
});

test("AC-6.7: a large token set chunks the session cookie, is logged by size only, and still works", async () => {
  issuer.accessPadding = 6000;
  let jar;
  try {
    ({ jar } = await login());
  } finally {
    issuer.accessPadding = 0;
  }
  assert.ok(
    jar.names().some((n) => n === "specflow-session.1"),
    `chunked cookie: ${jar.names()}`,
  );
  const res = await fetch(`${fe.base}/api/backend/me`, { headers: { cookie: jar.header() } });
  assert.equal(res.status, 200);
  assert.match(bearerSeenByApi(), /^Bearer at-\d+-x{6000}$/);
  const line = fe
    .output()
    .split("\n")
    .find((l) => l.includes("auth.session.cookie_chunked"));
  assert.ok(line, "an INFO line reports the chunked cookie");
  assert.deepEqual(Object.keys(JSON.parse(line)).sort(), ["bytes", "chunks", "event"]);
});

test("AC-6.8: N parallel requests near expiry cause exactly one token request (single-flight)", async () => {
  const { jar } = await login({ expiresIn: 1 }); // inside the refresh leeway at once
  issuer.accessExpiresIn = 3600;
  const before = issuer.refreshCount;
  const stale = jar.header();
  const results = await Promise.all(Array.from({ length: 8 }, () => fetch(`${fe.base}/api/backend/me`, { headers: { cookie: stale } })));
  assert.deepEqual(
    results.map((r) => r.status),
    Array(8).fill(200),
  );
  assert.equal(issuer.refreshCount - before, 1, "one refresh for 8 parallel requests");
  const refreshed = issuer.accessTokens.at(-1);
  assert.equal(bearerSeenByApi(), `Bearer ${refreshed}`);

  // The rotated cookie reaches the browser; later requests need no refresh.
  const fresh = new Jar();
  for (const r of results) fresh.absorb(r);
  assert.ok(fresh.names().length > 0, "a refreshed session cookie is set by the BFF");
  const again = await fetch(`${fe.base}/api/backend/me`, { headers: { cookie: fresh.header() } });
  assert.equal(again.status, 200);
  assert.equal(issuer.refreshCount - before, 1);

  // A request that still carries the old cookie reuses the cached result: the rotated
  // refresh token is not spent twice.
  const late = await fetch(`${fe.base}/api/backend/me`, { headers: { cookie: stale } });
  assert.equal(late.status, 200);
  assert.equal(issuer.refreshCount - before, 1);
  assert.equal(issuer.tokenRequests.filter((r) => r.grant_type === "refresh_token" && !r.ok).length, 0);
});

test("AC-6.8/6.9: a failed refresh clears the session (once, for parallel requests)", async () => {
  const { jar } = await login({ expiresIn: 1 });
  issuer.refreshMode = "fail";
  const before = issuer.refreshCount;
  try {
    const cookie = jar.header();
    const calls = await Promise.all(Array.from({ length: 4 }, () => fetch(`${fe.base}/api/backend/me`, { headers: { cookie } })));
    assert.deepEqual(
      calls.map((r) => r.status),
      Array(4).fill(401),
    );
    assert.equal(issuer.refreshCount - before, 1);
    const cleared = new Jar();
    cleared.absorb(calls[0]);
    // the Set-Cookie of the failed refresh deletes the session cookie
    assert.ok(calls[0].headers.getSetCookie().some((c) => /specflow-session/.test(c) && /Max-Age=0|Expires=Thu, 01 Jan 1970/i.test(c)));
    assert.equal(await sessionOf(fe.base, jar), null, "no session after a failed refresh");
    const page = await fetch(fe.base + "/", { headers: { cookie }, redirect: "manual" });
    assert.equal(page.status, 307);
  } finally {
    issuer.refreshMode = "ok";
  }
});

test("AC-6.9: logout ends the session", async () => {
  const { jar } = await login();
  assert.ok((await sessionOf(fe.base, jar))?.user);
  const token = await csrf(fe.base, jar);
  const out = await fetch(`${fe.base}/api/auth/signout`, {
    method: "POST",
    redirect: "manual",
    headers: { cookie: jar.header(), "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ csrfToken: token, callbackUrl: `${fe.base}/login` }),
  });
  jar.absorb(out);
  assert.equal(await sessionOf(fe.base, jar), null);
  const bff = await fetch(`${fe.base}/api/backend/me`, { headers: { cookie: jar.header() } });
  assert.equal(bff.status, 401);
  assert.equal((await fetch(fe.base + "/", { headers: { cookie: jar.header() }, redirect: "manual" })).status, 307);
});

test("AC-6.5: POST /api/auth/callback/credentials with a valid CSRF token yields no session in oidc mode", async () => {
  const jar = new Jar();
  const token = await csrf(fe.base, jar);
  const res = await fetch(`${fe.base}/api/auth/callback/credentials`, {
    method: "POST",
    redirect: "manual",
    headers: { cookie: jar.header(), "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ csrfToken: token, email: "attacker@example.test", callbackUrl: `${fe.base}/` }),
  });
  jar.absorb(res);
  assert.ok(!res.headers.getSetCookie().some((c) => /^specflow-session/.test(c) && !/Max-Age=0/i.test(c)), "no session cookie is set");
  assert.equal(await sessionOf(fe.base, jar), null);
  const bff = await fetch(`${fe.base}/api/backend/me`, { headers: { cookie: jar.header() } });
  assert.equal(bff.status, 401);
});

test("server logs carry no token, secret or cookie values", () => {
  const out = fe.output();
  assert.ok(!out.includes(CLIENT_SECRET));
  assert.doesNotMatch(out, /\bat-\d+\b|\brt-\d+\b|eyJ/);
});

/** Raw HTTP so a forged Host / X-Forwarded-Host really reaches the server (fetch forbids Host). */
function rawPost(port, path, headers, body) {
  return new Promise((resolve, reject) => {
    const req = request({ host: "127.0.0.1", port, path, method: "POST", headers }, (res) => {
      res.resume();
      res.on("end", () => resolve({ status: res.statusCode, location: res.headers.location ?? "" }));
    });
    req.on("error", reject);
    req.end(body);
  });
}

test("M5: the OIDC redirect_uri comes from PUBLIC_URL, not from a spoofed Host or X-Forwarded-Host", async () => {
  const jar = new Jar();
  const token = await csrf(fe.base, jar);
  const body = new URLSearchParams({ csrfToken: token, callbackUrl: `${fe.base}/` }).toString();
  const res = await rawPost(
    FE_PORT,
    "/api/auth/signin/oidc",
    {
      cookie: jar.header(),
      host: "evil.example.test",
      "x-forwarded-host": "evil.example.test",
      "x-forwarded-proto": "https",
      "content-type": "application/x-www-form-urlencoded",
      "content-length": Buffer.byteLength(body),
    },
    body,
  );
  assert.equal(res.status, 302, "sign-in still redirects to the issuer");
  const authorize = new URL(res.location);
  assert.equal(authorize.searchParams.get("redirect_uri"), `${fe.base}/api/auth/callback/oidc`);
  assert.doesNotMatch(res.location, /evil/, "no part of the redirect carries the forged host");
});
