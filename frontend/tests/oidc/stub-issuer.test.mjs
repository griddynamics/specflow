// The stub issuer and the stub API on their own (B-10, B-15): claims of the RS256 access token, JWKS, refresh keeps the
// identity, end-session on and off, and the API's `verify` mode. No frontend. Ports 3226-3228.
import crypto from "node:crypto";
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { SignJWT, createLocalJWKSet, decodeProtectedHeader, jwtVerify } from "jose";
import { randomSecret } from "../support/servers.mjs";
import { startStubApi } from "../support/stub-api.mjs";
import { startStubIssuer } from "./stub-issuer.mjs";

const ISSUER_PORT = 3226;
const API_PORT = 3227;
const OPEN_PORT = 3228;
const PUBLIC = `http://127.0.0.1:${ISSUER_PORT}/realms/stub`;
const CLIENT_ID = "specflow-web";
const CLIENT_SECRET = randomSecret();
const REDIRECT = "http://127.0.0.1:3299/api/auth/callback/oidc";
let issuer, api;

before(async () => {
  issuer = await startStubIssuer({
    port: ISSUER_PORT,
    publicIssuer: PUBLIC,
    clientId: CLIENT_ID,
    clientSecret: CLIENT_SECRET,
    defaultEmail: "default.user@example.test",
  });
  api = await startStubApi({ port: API_PORT, verify: { issuer: PUBLIC, audience: "specflow-api", jwks: issuer.jwks } });
});

after(async () => {
  await api?.close();
  await issuer?.close();
});

const OIDC = `${PUBLIC}/protocol/openid-connect`;
const form = (body) => ({
  method: "POST",
  headers: {
    "content-type": "application/x-www-form-urlencoded",
    authorization: `Basic ${Buffer.from(`${CLIENT_ID}:${CLIENT_SECRET}`).toString("base64")}`,
  },
  body: new URLSearchParams(body),
});

/** Authorization code + PKCE, as the app does it. */
async function signIn(loginHint) {
  const verifier = crypto.randomBytes(32).toString("base64url");
  const auth = new URL(`${OIDC}/auth`);
  for (const [k, v] of Object.entries({
    client_id: CLIENT_ID,
    response_type: "code",
    redirect_uri: REDIRECT,
    state: "s",
    nonce: "n",
    code_challenge: crypto.createHash("sha256").update(verifier).digest("base64url"),
    code_challenge_method: "S256",
  })) {
    auth.searchParams.set(k, v);
  }
  if (loginHint) auth.searchParams.set("login_hint", loginHint);
  const approved = await fetch(auth, { redirect: "manual" });
  assert.equal(approved.status, 302);
  const code = new URL(approved.headers.get("location")).searchParams.get("code");
  const res = await fetch(
    `${OIDC}/token`,
    form({ grant_type: "authorization_code", code, redirect_uri: REDIRECT, code_verifier: verifier }),
  );
  assert.equal(res.status, 200);
  return res.json();
}

const verifyAccess = (token, options = {}) =>
  jwtVerify(token, createLocalJWKSet(issuer.jwks), { issuer: PUBLIC, audience: "specflow-api", algorithms: ["RS256"], ...options });

test("the access token is an RS256 JWT with kid and the claims the backend checks", async () => {
  const t = await signIn("alice@example.test");
  assert.equal(decodeProtectedHeader(t.access_token).alg, "RS256");
  assert.equal(decodeProtectedHeader(t.access_token).kid, issuer.jwks.keys[0].kid);
  const { payload } = await verifyAccess(t.access_token);
  assert.equal(payload.iss, PUBLIC);
  assert.equal(payload.aud, "specflow-api");
  assert.equal(payload.azp, CLIENT_ID);
  assert.equal(payload.typ, "Bearer");
  assert.equal(payload.sub, "sub-alice@example.test");
  assert.equal(payload.email, "alice@example.test");
  assert.equal(payload.email_verified, true);
  assert.equal(payload.name, "alice");
  assert.ok(payload.jti && payload.iat);
  assert.equal(payload.exp - payload.iat, 3600);
  assert.deepEqual(issuer.accessTokens.at(-1), t.access_token);
});

test("the JWKS endpoint serves the verification key and no private part", async () => {
  const jwks = await (await fetch(`${OIDC}/certs`)).json();
  assert.deepEqual(jwks, issuer.jwks);
  assert.equal(jwks.keys[0].alg, "RS256");
  assert.equal(jwks.keys[0].use, "sig");
  assert.equal(jwks.keys[0].d, undefined);
});

test("without a login_hint the default identity signs in", async () => {
  const { access_token } = await signIn();
  assert.equal((await verifyAccess(access_token)).payload.email, "default.user@example.test");
});

test("the id_token carries the nonce and the client as audience", async () => {
  const t = await signIn("alice@example.test");
  const { payload } = await jwtVerify(t.id_token, createLocalJWKSet(issuer.jwks), { issuer: PUBLIC, audience: CLIENT_ID });
  assert.equal(payload.nonce, "n");
  assert.equal(payload.email, "alice@example.test");
});

test("B-10: a refresh keeps the signed-in user, returns a fresh id_token, and rotates the refresh token", async () => {
  const first = await signIn("bob@example.test");
  const second = await (await fetch(`${OIDC}/token`, form({ grant_type: "refresh_token", refresh_token: first.refresh_token }))).json();
  assert.equal((await verifyAccess(second.access_token)).payload.email, "bob@example.test");
  assert.equal((await verifyAccess(second.access_token)).payload.sub, "sub-bob@example.test");
  assert.ok(second.id_token, "the refresh response has an id_token");
  assert.equal(
    (await jwtVerify(second.id_token, createLocalJWKSet(issuer.jwks), { issuer: PUBLIC, audience: CLIENT_ID })).payload.email,
    "bob@example.test",
  );
  assert.notEqual(second.refresh_token, first.refresh_token);
  const replay = await fetch(`${OIDC}/token`, form({ grant_type: "refresh_token", refresh_token: first.refresh_token }));
  assert.equal(replay.status, 400, "a spent refresh token is refused");
});

test("accessExpiresIn and accessPadding shape the access token", async () => {
  issuer.accessExpiresIn = 120;
  issuer.accessPadding = 5000;
  try {
    const { access_token } = await signIn("carol@example.test");
    const { payload } = await verifyAccess(access_token);
    assert.equal(payload.exp - payload.iat, 120);
    assert.ok(access_token.length > 5000);
  } finally {
    issuer.accessExpiresIn = 3600;
    issuer.accessPadding = 0;
  }
});

test("end session: advertised, recorded, redirects only to the client's origin, and switchable", async () => {
  const t = await signIn("dora@example.test");
  const disco = await (await fetch(`${PUBLIC}/.well-known/openid-configuration`)).json();
  assert.equal(disco.end_session_endpoint, `${OIDC}/logout`);

  const before = issuer.endSessionRequests.length;
  const back = "http://127.0.0.1:3299/login?signedout=1";
  const q = new URLSearchParams({ id_token_hint: t.id_token, post_logout_redirect_uri: back, client_id: CLIENT_ID });
  const ok = await fetch(`${disco.end_session_endpoint}?${q}`, { redirect: "manual" });
  assert.equal(ok.status, 302);
  assert.equal(ok.headers.get("location"), back);
  assert.equal(issuer.endSessionRequests.length, before + 1);
  assert.deepEqual(issuer.endSessionRequests.at(-1), {
    idTokenHint: t.id_token,
    idTokenHintValid: true,
    postLogoutRedirectUri: back,
    clientId: CLIENT_ID,
    ok: true,
  });

  const foreign = await fetch(
    `${disco.end_session_endpoint}?${new URLSearchParams({ post_logout_redirect_uri: "https://evil.example.test/" })}`,
    {
      redirect: "manual",
    },
  );
  assert.equal(foreign.status, 400, "a foreign redirect is refused");
  assert.equal(issuer.endSessionRequests.at(-1).ok, false);
  assert.equal(issuer.endSessionRequests.at(-1).idTokenHintValid, false);

  issuer.endSession = false;
  try {
    const gone = await (await fetch(`${PUBLIC}/.well-known/openid-configuration`)).json();
    assert.equal("end_session_endpoint" in gone, false);
    const res = await fetch(`${disco.end_session_endpoint}?${q}`, { redirect: "manual" });
    assert.equal(res.status, 404);
  } finally {
    issuer.endSession = true;
  }
});

test("stub API verify mode accepts the issuer's token and refuses anything else", async () => {
  const get = (token) =>
    fetch(`http://127.0.0.1:${API_PORT}/api/v1/me`, { headers: token === undefined ? {} : { authorization: `Bearer ${token}` } });
  const { access_token } = await signIn("eve@example.test");
  assert.equal((await get(access_token)).status, 200);
  assert.equal((await get(undefined)).status, 401);
  assert.equal((await get("not-a-jwt")).status, 401);

  const claims = { email: "eve@example.test", azp: CLIENT_ID, typ: "Bearer" };
  const forge = (key, alg, { aud = "specflow-api", iss = PUBLIC, exp = "5m" } = {}) =>
    new SignJWT(claims)
      .setProtectedHeader({ alg })
      .setIssuer(iss)
      .setAudience(aud)
      .setSubject("sub-eve")
      .setIssuedAt()
      .setExpirationTime(exp)
      .sign(key);
  const hmac = new TextEncoder().encode(randomSecret());
  const { privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
  assert.equal((await get(await forge(hmac, "HS256"))).status, 401, "HS256 is refused");
  assert.equal((await get(await forge(privateKey, "RS256"))).status, 401, "a token signed by another key is refused");

  // Signed by the issuer's own key but already past its expiry.
  issuer.accessExpiresIn = -10;
  try {
    const { access_token: expired } = await signIn("eve@example.test");
    assert.equal((await get(expired)).status, 401, "an expired token is refused");
  } finally {
    issuer.accessExpiresIn = 3600;
  }
});

test("stub API open mode accepts any bearer", async () => {
  const open = await startStubApi({ port: OPEN_PORT });
  try {
    const res = await fetch(`http://127.0.0.1:${OPEN_PORT}/api/v1/me`, { headers: { authorization: "Bearer anything" } });
    assert.equal(res.status, 200);
  } finally {
    await open.close();
  }
});
