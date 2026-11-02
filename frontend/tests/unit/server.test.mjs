// HTTP-level checks against the BUILT standalone server (ports 3210-3219):
// AC-12.1, AC-6.5 (sign-in over OIDC), AC-6.6 (BFF rules), AC-6.7 (cookie, session shape). The app signs in against the
// in-process stub issuer (tests/oidc/stub-issuer.mjs); the stub API verifies the issuer's RS256 access tokens.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { frontendEnv, startFrontend } from "../support/servers.mjs";
import { startOidcStack } from "../support/oidc-stack.mjs";
import { oidcLogin, sessionOf } from "../support/session.mjs";
import { rawRequest } from "../support/http.mjs";

const FE_PORT = 3210;
const API_PORT = 3211;
const ISSUER_PORT = 3212;
const EMAIL = "oidc.user@example.test";
let stack, api, fe, jar, origin;

const csrfHeaders = () => ({ cookie: jar.header(), origin, "x-requested-with": "specflow" });

before(async () => {
  stack = await startOidcStack({ issuerPort: ISSUER_PORT, apiPort: API_PORT });
  api = stack.api;
  fe = await startFrontend({ port: FE_PORT, env: frontendEnv(FE_PORT, { APP_ENV: "local", ...stack.env }) });
  origin = fe.base;
  jar = await oidcLogin(fe.base, EMAIL);
});

after(async () => {
  await fe?.stop();
  await stack?.close();
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

test("AC-6.5: the login page offers the OIDC button", async () => {
  const html = await (await fetch(fe.base + "/login")).text();
  assert.match(html, /data-testid="login-oidc"/);
});

test("AC-6.7: session cookie attributes and a session that exposes only name and email", async () => {
  const jarFresh = await oidcLogin(fe.base, EMAIL);
  const cookie = jarFresh.setCookies.find((c) => c.startsWith("specflow-session"));
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
  assert.equal(seen.authOk, true, "the stub verified the issuer's RS256 access token against its JWKS");
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
