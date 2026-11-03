// AC-16.8 / AC-6.9: OIDC contract test. Local Node issuer + the built server with
// OIDC_INTERNAL_URL (the public issuer host does not resolve, so every back-channel call must
// go through the internal URL). The stub API verifies the bearer against the issuer's JWKS. Ports 3220-3229.
// Covers AC-6.5 (credentials callback yields no session), AC-6.8 (single-flight refresh,
// failed refresh clears the session), AC-6.9 (login -> session, refresh, logout), B-08/B-09 (RP-initiated logout with and without
// an end_session_endpoint). Ports 3220-3229.
import { createServer, request } from "node:http";
import { test, before, after } from "node:test";
import { decodeJwt } from "jose";
import assert from "node:assert/strict";
import { frontendEnv, randomSecret, startFrontend } from "../support/servers.mjs";
import { startStubApi } from "../support/stub-api.mjs";
import { API_AUDIENCE } from "../support/oidc-stack.mjs";
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
  api = await startStubApi({ port: API_PORT, verify: { issuer: PUBLIC_ISSUER, audience: API_AUDIENCE, jwks: issuer.jwks } });
  fe = await startFrontend({
    port: FE_PORT,
    env: frontendEnv(FE_PORT, {
      APP_ENV: "local",
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
  await issuer?.close().catch(() => undefined); // already down after the last test
});

const bearerSeenByApi = () => api.log.at(-1).headers.authorization;
/** The claims of the access token the API last verified (the API only logs it after checking signature, issuer and audience). */
const claimsSeenByApi = () => {
  assert.equal(api.log.at(-1).authOk, true, "the stub API verified the bearer against the issuer JWKS");
  return decodeJwt(bearerSeenByApi().slice(7));
};

/** Sign in the way a browser does; `expiresIn` shapes the first access token. */
async function login({ expiresIn = 3600, email = "oidc.user@example.test", app = fe } = {}) {
  issuer.accessExpiresIn = expiresIn;
  const jar = new Jar();
  const token = await csrf(app.base, jar);
  const start = await fetch(`${app.base}/api/auth/signin/oidc`, {
    method: "POST",
    redirect: "manual",
    headers: { cookie: jar.header(), "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ csrfToken: token, callbackUrl: `${app.base}/` }),
  });
  jar.absorb(start);
  assert.equal(start.status, 302);
  const authorize = new URL(start.headers.get("location"));
  authorize.searchParams.set("login_hint", email); // the stub issuer signs this user in
  // The "browser" cannot resolve the public host: talk to the stub directly.
  const approved = await fetch(`http://127.0.0.1:${ISSUER_PORT}${authorize.pathname}${authorize.search}`, { redirect: "manual" });
  assert.equal(approved.status, 302);
  const callback = await fetch(approved.headers.get("location"), { redirect: "manual", headers: { cookie: jar.header() } });
  jar.absorb(callback);
  assert.equal(callback.status, 302, "callback redirects after a successful login");
  return { jar, authorize };
}

test("AC-6.5/R3.2: the login page shows one neutral Sign in OIDC button and no email form", async () => {
  const html = await (await fetch(fe.base + "/login")).text();
  assert.match(html, /data-testid="login-oidc"/);
  assert.match(html, />Sign in</);
  assert.doesNotMatch(html, /Google|Keycloak/, "the label names no identity provider");
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
  const shown = JSON.stringify(session);
  for (const value of [...issuer.accessTokens, ...issuer.idTokens])
    assert.ok(!shown.includes(value), "the session carries no issued token");
  assert.doesNotMatch(shown, /rt-\d|eyJ/);
  assert.ok(jar.names().includes("specflow-session") || jar.names().some((n) => n.startsWith("specflow-session.")));

  const res = await fetch(`${fe.base}/api/backend/me`, { headers: { cookie: jar.header() } });
  assert.equal(res.status, 200);
  assert.equal(bearerSeenByApi(), `Bearer ${issuer.accessTokens.at(-1)}`);
  assert.equal(claimsSeenByApi().email, "oidc.user@example.test");
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
  assert.equal(bearerSeenByApi(), `Bearer ${issuer.accessTokens.at(-1)}`);
  assert.ok(bearerSeenByApi().length > 6000, "the padded token went through the chunked cookie whole");
  claimsSeenByApi(); // verified by the stub API
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

test("B-10: a refresh keeps the signed-in user's identity", async () => {
  const { jar } = await login({ expiresIn: 1, email: "carol.refresh@example.test" });
  issuer.accessExpiresIn = 3600;
  const before = issuer.refreshCount;
  const res = await fetch(`${fe.base}/api/backend/me`, { headers: { cookie: jar.header() } });
  assert.equal(res.status, 200);
  assert.equal(issuer.refreshCount - before, 1, "the access token was refreshed");
  const claims = claimsSeenByApi();
  assert.equal(claims.email, "carol.refresh@example.test");
  assert.equal(claims.sub, "sub-carol.refresh@example.test");
  assert.equal((await sessionOf(fe.base, jar)).user.email, "carol.refresh@example.test");
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

const endSessionCall = (jar) => fetch(`${fe.base}/api/auth/end-session`, { method: "POST", headers: { cookie: jar.header() } });

test("B-08: with an end_session_endpoint, the end-session route answers the issuer's logout URL and the IdP session ends", async () => {
  const { jar } = await login();
  const res = await endSessionCall(jar);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  const body = await res.json();
  assert.deepEqual(Object.keys(body), ["url"], "nothing but the URL is returned");
  const url = new URL(body.url);
  assert.equal(url.origin + url.pathname, `${PUBLIC_ISSUER}/protocol/openid-connect/logout`);
  assert.equal(url.searchParams.get("id_token_hint"), issuer.idTokens.at(-1), "the id_token of this sign-in");
  assert.equal(url.searchParams.get("post_logout_redirect_uri"), `${fe.base}/login?signedout=1`);
  assert.equal(url.searchParams.get("client_id"), CLIENT_ID);
  assert.ok(!body.url.includes(issuer.accessTokens.at(-1)) && !body.url.includes("rt-"), "no access or refresh token in the URL");

  // The browser follows it (the public host does not resolve here: talk to the stub directly).
  const before = issuer.endSessionRequests.length;
  const idp = await fetch(`http://127.0.0.1:${ISSUER_PORT}${url.pathname}${url.search}`, { redirect: "manual" });
  assert.equal(idp.status, 302);
  assert.equal(idp.headers.get("location"), `${fe.base}/login?signedout=1`);
  assert.equal(issuer.endSessionRequests.length - before, 1);
  assert.deepEqual(
    { ...issuer.endSessionRequests.at(-1), idTokenHint: undefined },
    {
      idTokenHint: undefined,
      idTokenHintValid: true,
      postLogoutRedirectUri: `${fe.base}/login?signedout=1`,
      clientId: CLIENT_ID,
      ok: true,
    },
  );
  const session = await sessionOf(fe.base, jar);
  assert.deepEqual(Object.keys(session.user).sort(), ["email", "name"], "the id_token never reaches the session JSON");
  assert.ok(!JSON.stringify(session).includes(issuer.idTokens.at(-1)));
});

test("B-08: a refresh replaces the stored id_token, so the logout hint is the newest one", async () => {
  const { jar } = await login({ expiresIn: 1, email: "dave.refresh@example.test" });
  issuer.accessExpiresIn = 3600;
  const first = issuer.idTokens.at(-1);
  const res = await fetch(`${fe.base}/api/backend/me`, { headers: { cookie: jar.header() } });
  assert.equal(res.status, 200);
  const rotated = new Jar();
  rotated.absorb(res);
  const second = issuer.idTokens.at(-1);
  assert.notEqual(second, first, "the refresh brought a new id_token");
  const { url } = await (await endSessionCall(rotated)).json();
  assert.equal(new URL(url).searchParams.get("id_token_hint"), second);
});

test("B-08: the return address comes from PUBLIC_URL only: no query, header or Host reaches it", async () => {
  const { jar } = await login();
  const res = await fetch(
    `${fe.base}/api/auth/end-session?post_logout_redirect_uri=https://evil.example.test/&callbackUrl=https://evil.example.test/`,
    {
      method: "POST",
      headers: {
        cookie: jar.header(),
        "x-forwarded-host": "evil.example.test",
        "x-forwarded-proto": "https",
        "content-type": "application/json",
      },
      body: JSON.stringify({ url: "https://evil.example.test/", post_logout_redirect_uri: "https://evil.example.test/" }),
    },
  );
  const { url } = await res.json();
  assert.doesNotMatch(url, /evil/);
  assert.equal(new URL(url).searchParams.get("post_logout_redirect_uri"), `${fe.base}/login?signedout=1`);
});

test("B-08: without a session the end-session route answers the local sign-out URL; GET is not allowed", async () => {
  const res = await endSessionCall(new Jar());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.deepEqual(await res.json(), { url: `${fe.base}/login?signedout=1` });
  const get = await fetch(`${fe.base}/api/auth/end-session`, { redirect: "manual" });
  assert.equal(get.status, 405, "the static route handles it, not the Auth.js catch-all");
});

test("B-09: without an end_session_endpoint (Google) the end-session route answers the local sign-out URL and nothing reaches the IdP", async () => {
  const { jar } = await login();
  issuer.endSession = false; // the issuer stops advertising it: discovery is read at sign-out, not remembered
  try {
    const discovery = await (await fetch(`${issuer.url}/.well-known/openid-configuration`)).json();
    assert.equal(discovery.end_session_endpoint, undefined, "the issuer advertises none");
    const before = issuer.endSessionRequests.length;
    const res = await endSessionCall(jar);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { url: `${fe.base}/login?signedout=1` });
    assert.equal(issuer.endSessionRequests.length, before, "nothing reached the IdP");
  } finally {
    issuer.endSession = true;
  }
  // The local sign-out still ends the session.
  const token = await csrf(fe.base, jar);
  jar.absorb(
    await fetch(`${fe.base}/api/auth/signout`, {
      method: "POST",
      redirect: "manual",
      headers: { cookie: jar.header(), "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ csrfToken: token, callbackUrl: `${fe.base}/login` }),
    }),
  );
  assert.equal(await sessionOf(fe.base, jar), null);
});

test("AC-6.5: there is no credentials provider: POST /api/auth/callback/credentials with a valid CSRF token yields no session", async () => {
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
  assert.ok(issuer.accessTokens.length > 0 && issuer.idTokens.length > 0, "tokens were issued during the run");
  for (const value of [...issuer.accessTokens, ...issuer.idTokens]) assert.ok(!out.includes(value), "no issued token value in the logs");
  assert.doesNotMatch(out, /\brt-\d+\b/);
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

/**
 * A second app whose back-channel discovery goes through a proxy to the stub issuer. `mode` shapes only the discovery answer:
 * "ok" passes it through, "foreign" advertises an end_session_endpoint on another origin, "redirect" answers 302.
 */
async function startDiscoveryProxy(port) {
  const proxy = { mode: "ok", discoveryCalls: 0 };
  const server = createServer(async (req, res) => {
    const isDiscovery = req.url.endsWith("/.well-known/openid-configuration");
    if (isDiscovery) proxy.discoveryCalls += 1;
    if (isDiscovery && proxy.mode === "redirect") {
      res.writeHead(302, { location: `${issuer.url}/.well-known/openid-configuration` });
      return res.end();
    }
    const body = ["GET", "HEAD"].includes(req.method) ? undefined : Buffer.concat(await req.toArray());
    const upstream = await fetch(`http://127.0.0.1:${ISSUER_PORT}${req.url}`, {
      method: req.method,
      headers: { "content-type": req.headers["content-type"] ?? "", authorization: req.headers.authorization ?? "" },
      body,
    });
    let text = await upstream.text();
    if (isDiscovery && proxy.mode === "foreign") {
      text = JSON.stringify({ ...JSON.parse(text), end_session_endpoint: "http://evil.example.test/logout" });
    }
    res.writeHead(upstream.status, { "content-type": upstream.headers.get("content-type") ?? "application/json" });
    res.end(text);
  });
  await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));
  return Object.assign(proxy, { url: `http://127.0.0.1:${port}/realms/stub`, close: () => new Promise((r) => server.close(r)) });
}

test("B-08 hardening: a discovery that advertises a foreign-origin endpoint, or redirects, yields the local sign-out URL (never the id_token off-origin)", async () => {
  const proxy = await startDiscoveryProxy(3224);
  const app = await startFrontend({
    port: 3222,
    env: frontendEnv(3222, {
      APP_ENV: "local",
      API_INTERNAL_URL: api.url,
      SESSION_SECRET: randomSecret(),
      OIDC_ISSUER: PUBLIC_ISSUER,
      OIDC_INTERNAL_URL: proxy.url,
      OIDC_CLIENT_ID: CLIENT_ID,
      OIDC_CLIENT_SECRET: CLIENT_SECRET,
    }),
  });
  try {
    const { jar } = await login({ app });
    const call = async () =>
      (await fetch(`${app.base}/api/auth/end-session`, { method: "POST", headers: { cookie: jar.header() } })).json();
    const fallback = { url: `${app.base}/login?signedout=1` };
    assert.match((await call()).url, /\/protocol\/openid-connect\/logout\?/, "control: the same-origin endpoint is used");

    proxy.mode = "foreign";
    const out = app.output().length;
    const foreign = await call();
    assert.deepEqual(foreign, fallback);
    assert.match(app.output().slice(out), /"event":"auth\.signout\.endpoint_origin_mismatch"/);
    assert.ok(!app.output().slice(out).includes("evil.example.test"), "no URL in the log");

    proxy.mode = "redirect";
    const out2 = app.output().length;
    assert.deepEqual(await call(), fallback, "a redirected discovery is not followed");
    assert.match(app.output().slice(out2), /"event":"auth\.signout\.idp_unavailable"/);

    proxy.mode = "ok";
    const calls = proxy.discoveryCalls;
    const res = await fetch(`${app.base}/api/auth/end-session`, {
      method: "POST",
      headers: { cookie: jar.header(), "sec-fetch-site": "cross-site" },
    });
    assert.deepEqual(await res.json(), fallback, "a cross-site caller gets the fallback");
    assert.equal(proxy.discoveryCalls, calls, "...without a discovery fetch");
    assert.deepEqual(await (await fetch(`${app.base}/api/auth/end-session`, { method: "POST" })).json(), fallback);
    assert.equal(proxy.discoveryCalls, calls, "no session: no discovery fetch");
  } finally {
    await app.stop();
    await proxy.close();
  }
});

test("B-09 (keep last): an unreachable issuer at sign-out falls back to the local sign-out URL, logged without values", async () => {
  const { jar } = await login();
  const out = fe.output().length;
  await issuer.close(); // last test of the file: the issuer stays down
  const anonymous = await endSessionCall(new Jar());
  assert.deepEqual(await anonymous.json(), { url: `${fe.base}/login?signedout=1` });
  assert.doesNotMatch(fe.output().slice(out), /idp_unavailable/, "no session: discovery was not even tried");
  const res = await endSessionCall(jar);
  assert.deepEqual(await res.json(), { url: `${fe.base}/login?signedout=1` });
  assert.match(fe.output().slice(out), /"event":"auth\.signout\.idp_unavailable"/);
});
