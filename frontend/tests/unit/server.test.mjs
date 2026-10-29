// HTTP-level checks against the BUILT standalone server (ports 3210-3219):
// AC-12.1, AC-6.4 (startup), AC-6.5 (dev form), AC-6.6 (BFF rules), AC-6.7 (cookie, session shape).
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { frontendEnv, randomSecret, startFrontend } from "../support/servers.mjs";
import { startStubApi } from "../support/stub-api.mjs";
import { devLogin, sessionOf } from "../support/session.mjs";
import { rawRequest } from "../support/http.mjs";

const FE_PORT = 3210;
const API_PORT = 3211;
const BAD_PORT = 3212;
const KEY = randomSecret();
const EMAIL = "dev.user@example.test";
let api, fe, jar, origin;

const csrfHeaders = () => ({ cookie: jar.header(), origin, "x-requested-with": "specflow" });

before(async () => {
  api = await startStubApi({ port: API_PORT, signingKey: KEY });
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
  origin = fe.base;
  jar = await devLogin(fe.base, EMAIL);
});

after(async () => {
  await fe?.stop();
  await api?.close();
});

test("AC-12.1: unauthenticated page requests redirect to /login", async () => {
  for (const p of ["/", "/projects/abc", "/projects/abc/documents/def"]) {
    const res = await fetch(fe.base + p, { redirect: "manual" });
    assert.equal(res.status, 307, p);
    assert.equal(new URL(res.headers.get("location"), fe.base).href, `${fe.base}/login`, p);
  }
  assert.equal((await fetch(fe.base + "/login", { redirect: "manual" })).status, 200);
});

test("AC-12.1: /api/* is not matched by the proxy (health, backend, auth answer themselves)", async () => {
  assert.equal((await fetch(fe.base + "/api/health", { redirect: "manual" })).status, 200);
  const bff = await fetch(fe.base + "/api/backend/me", { redirect: "manual" });
  assert.equal(bff.status, 401); // the route handler, not a 307 to /login
  assert.equal((await bff.json()).error.code, "unauthenticated");
  assert.equal((await fetch(fe.base + "/api/auth/session", { redirect: "manual" })).status, 200);
});

test("AC-6.5: the dev form exists in dev mode, prefilled by LOCAL_TEST_USER_EMAIL; no OIDC button", async () => {
  const html = await (await fetch(fe.base + "/login")).text();
  assert.match(html, /data-testid="login-dev-email"/);
  assert.match(html, /data-testid="login-dev-submit"/);
  assert.ok(html.includes(EMAIL));
  assert.doesNotMatch(html, /login-oidc/);
});

test("AC-6.7: session cookie attributes and a session that exposes only name and email", async () => {
  const jarFresh = await devLogin(fe.base, EMAIL);
  const csrf = await (await fetch(`${fe.base}/api/auth/csrf`, { headers: { cookie: jarFresh.header() } })).json();
  const res = await fetch(`${fe.base}/api/auth/callback/credentials`, {
    method: "POST",
    redirect: "manual",
    headers: { cookie: jarFresh.header(), "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ csrfToken: csrf.csrfToken, email: EMAIL }),
  });
  const cookie = res.headers.getSetCookie().find((c) => c.startsWith("specflow-session"));
  assert.ok(cookie, "session cookie is named specflow-session on http");
  assert.match(cookie, /HttpOnly/i);
  assert.match(cookie, /SameSite=Lax/i);
  assert.doesNotMatch(cookie, /Secure/i);
  const body = await sessionOf(fe.base, jar);
  assert.deepEqual(Object.keys(body).sort(), ["expires", "user"]);
  assert.deepEqual(Object.keys(body.user).sort(), ["email", "name"]);
  assert.doesNotMatch(JSON.stringify(body), /eyJ/); // no JWT of any kind
});

test("AC-6.6: `..` in any spelling never reaches the API (400 from the BFF, or already resolved by Next)", async () => {
  const before = api.log.length;
  // Next decodes and resolves %2e%2e itself (-> another route, 404); the rest reach the handler.
  const handled = ["/api/backend/a%2fb", "/api/backend/projects/%252e%252e/x", "/api/backend/%2e%2e%2fme", "/api/backend/a%5cb"];
  for (const p of handled) {
    assert.equal((await rawRequest({ base: fe.base, path: p, headers: csrfHeaders() })).status, 400, p);
  }
  for (const p of ["/api/backend/%2e%2e/me", "/api/backend/projects/%2E%2E/x"]) {
    assert.ok([400, 404].includes((await rawRequest({ base: fe.base, path: p, headers: csrfHeaders() })).status), p);
  }
  assert.ok(
    api.log.slice(before).every((e) => !e.path.includes("..")),
    "the API never saw ..",
  );
});

test("AC-6.6: a literal `..` is normalised by the server before the handler and cannot leave /api/v1", async () => {
  const before = api.log.length;
  await rawRequest({ base: fe.base, path: "/api/backend/projects/../me", headers: csrfHeaders() });
  const seen = api.log.slice(before).at(-1);
  assert.ok(seen.path.startsWith("/api/v1/") && !seen.path.includes(".."), seen.path);
});

test("AC-6.6: non-GET needs Origin == PUBLIC_URL and X-Requested-With: specflow", async () => {
  const post = (headers) =>
    rawRequest({
      base: fe.base,
      path: "/api/backend/projects",
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: JSON.stringify({ name: "x" }),
    });
  assert.equal((await post({ cookie: jar.header() })).status, 403);
  assert.equal((await post({ cookie: jar.header(), origin: "http://evil.example.test", "x-requested-with": "specflow" })).status, 403);
  assert.equal((await post({ cookie: jar.header(), origin, "x-requested-with": "XMLHttpRequest" })).status, 403);
  assert.equal((await post({ cookie: jar.header(), origin })).status, 403);
  const ok = await post(csrfHeaders());
  assert.equal(ok.status, 201);
});

test("AC-6.6: no session means 401, even with valid CSRF headers", async () => {
  const res = await rawRequest({
    base: fe.base,
    path: "/api/backend/projects",
    method: "POST",
    headers: { origin, "x-requested-with": "specflow", "content-type": "application/json" },
    body: "{}",
  });
  assert.equal(res.status, 401);
});

test("AC-6.6: the BFF strips cookie/authorization/host/x-forwarded-*, adds the session bearer, keeps path and query", async () => {
  const before = api.log.length;
  const res = await rawRequest({
    base: fe.base,
    path: "/api/backend/projects?limit=7",
    headers: {
      cookie: jar.header(),
      authorization: "Bearer attacker",
      "x-forwarded-for": "6.6.6.6",
      "x-forwarded-host": "evil.test",
      "x-forwarded-proto": "https",
      origin,
      "x-http-method-override": "DELETE",
      "x-method-override": "DELETE",
      "x-original-url": "/admin",
      "x-rewrite-url": "/admin",
    },
  });
  assert.equal(res.status, 200);
  const seen = api.log.slice(before).at(-1);
  assert.equal(seen.path, "/api/v1/projects?limit=7");
  assert.equal(seen.authOk, true, "the stub verified the minted HS256 dev token");
  assert.notEqual(seen.headers.authorization, "Bearer attacker");
  assert.ok(!("cookie" in seen.headers));
  assert.ok(!Object.keys(seen.headers).some((h) => h.startsWith("x-forwarded-")));
  assert.equal(seen.headers.host, `127.0.0.1:${API_PORT}`);
  assert.ok(!("origin" in seen.headers));
  for (const h of ["x-http-method-override", "x-method-override", "x-original-url", "x-rewrite-url"]) assert.ok(!(h in seen.headers), h);
});

test("AC-6.6: upstream errors are passed through; unreachable upstream is a 502 JSON", async () => {
  const res = await rawRequest({
    base: fe.base,
    path: "/api/backend/projects/00000000-0000-0000-0000-000000000000",
    headers: csrfHeaders(),
  });
  assert.equal(res.status, 404);
  assert.equal(JSON.parse(res.text).error.code, "project_not_found");
});

test("the dev token the BFF forwards carries the architecture-notes claims", async () => {
  const { jwtVerify } = await import("jose");
  const seen = api.log.filter((e) => e.authOk).at(-1);
  const { payload, protectedHeader } = await jwtVerify(seen.headers.authorization.slice(7), new TextEncoder().encode(KEY), {
    issuer: "specflow-dev-login",
    audience: "specflow-api",
  });
  assert.equal(protectedHeader.alg, "HS256");
  assert.equal(payload.azp, "specflow-web");
  assert.equal(payload.typ, "Bearer");
  assert.equal(payload.email, EMAIL);
  assert.equal(payload.email_verified, true);
  assert.equal(payload.sub, `dev:${EMAIL}`);
  assert.equal(payload.exp - payload.iat, 8 * 3600);
});

test("AC-6.4: dev mode outside APP_ENV=local stops the server at startup, naming the variable", async () => {
  await assert.rejects(
    startFrontend({
      port: BAD_PORT,
      startTimeoutMs: 8000,
      env: frontendEnv(BAD_PORT, {
        APP_ENV: "dev",
        AUTH_MODE: "dev",
        API_INTERNAL_URL: api.url,
        SESSION_SECRET: randomSecret(),
        AUTH_DEV_SIGNING_KEY: randomSecret(),
      }),
    }),
    (e) => /AUTH_MODE/.test(e.message) && !e.message.includes(KEY),
  );
});
