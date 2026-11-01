// A small local OIDC issuer for the frontend test harnesses (AC-6.9, B-15): discovery, JWKS, authorize
// (auto-approve, redirects back with code and state), token (authorization_code with PKCE S256, refresh_token
// with one-time rotation; access tokens are RS256 JWTs with the claims the backend checks), userinfo and
// RP-initiated logout. The signing key is generated at start and never leaves the process. Test double only.
// Logout rule: `post_logout_redirect_uri` must be on the origin of the app (`appOrigin`) or of a redirect_uri seen at sign-in, else
// 400. `id_token_hint` is only observed (recorded, with `idTokenHintValid`: signed by this issuer for the client), never enforced.
import http from "node:http";
import crypto from "node:crypto";
import { SignJWT, createLocalJWKSet, exportJWK, generateKeyPair, jwtVerify } from "jose";
import { close, listen } from "../support/servers.mjs";

const b64url = (buf) => Buffer.from(buf).toString("base64url");
const json = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
};

/**
 * @param {{port:number, publicIssuer:string, clientId:string, clientSecret:string, path?:string,
 *   audience?:string, defaultEmail?:string, endSession?:boolean, appOrigin?:string}} o
 * `publicIssuer` is the issuer string put in tokens and discovery (what browsers see);
 * the server itself answers on `127.0.0.1:port` under `path`. `audience` is the access token `aud`;
 * `defaultEmail` signs in when the authorize request has no `login_hint`; `endSession` advertises the logout endpoint;
 * `appOrigin` is an origin a logout may return to even before any sign-in.
 */
export async function startStubIssuer({
  port,
  publicIssuer,
  clientId,
  clientSecret,
  path = "/realms/stub",
  audience = "specflow-api",
  defaultEmail = "oidc.user@example.test",
  endSession = true,
  appOrigin,
}) {
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  const jwk = { ...(await exportJWK(publicKey)), kid: "stub-key-1", alg: "RS256", use: "sig" };
  const codes = new Map(); // code -> {nonce, challenge, redirectUri, email}
  const liveRefresh = new Map(); // refresh token -> the email it was issued to
  const redirectOrigins = new Set(appOrigin ? [new URL(appOrigin).origin] : []); // where a logout may return to; sign-in adds the redirect_uri origins
  let counter = 0;

  const issuer = {
    url: `http://127.0.0.1:${port}${path}`,
    jwks: { keys: [jwk] },
    // controls (mutated by the test)
    endSession, // false: no end_session_endpoint in discovery and the logout route is 404 (the Google case)
    accessExpiresIn: 3600,
    accessClaims: {}, // test control: claims merged over the access token's (a key set to undefined drops the claim)
    accessPadding: 0, // extra characters in the access token, to force a chunked session cookie
    refreshMode: "ok", // "ok" | "fail"
    refreshDelayMs: 250,
    // observations
    authorizeRequests: [],
    tokenRequests: [], // {grant_type, ok}
    get refreshCount() {
      return issuer.tokenRequests.filter((r) => r.grant_type === "refresh_token").length;
    },
    accessTokens: [], // JWT strings
    idTokens: [], // JWT strings
    endSessionRequests: [], // {idTokenHint?, idTokenHintValid, postLogoutRedirectUri, clientId?, ok}
  };

  const sign = (claims, expiresInSeconds) =>
    new SignJWT(claims)
      .setProtectedHeader({ alg: "RS256", kid: jwk.kid })
      .setIssuer(publicIssuer)
      .setSubject(`sub-${claims.email}`)
      .setIssuedAt(claims.iat) // accessClaims may set `iat`
      .setExpirationTime(`${expiresInSeconds}s`)
      .sign(privateKey);

  async function tokens(email, nonce) {
    counter += 1;
    const profile = { email, email_verified: true, name: email.split("@")[0] };
    const access = await sign(
      {
        ...profile,
        aud: audience,
        azp: clientId,
        typ: "Bearer",
        jti: crypto.randomUUID(),
        ...(issuer.accessPadding ? { pad: "x".repeat(issuer.accessPadding) } : {}), // unchecked claim: grows the token only
        ...issuer.accessClaims,
      },
      issuer.accessExpiresIn,
    );
    const refresh = `rt-${counter}`;
    liveRefresh.set(refresh, email);
    issuer.accessTokens.push(access);
    const idToken = await sign({ ...profile, aud: clientId, ...(nonce ? { nonce } : {}) }, 300);
    issuer.idTokens.push(idToken);
    return { access_token: access, refresh_token: refresh, id_token: idToken, token_type: "Bearer", expires_in: issuer.accessExpiresIn };
  }

  /** True when `hint` is an id_token this issuer signed for the client. */
  async function ownIdToken(hint) {
    try {
      await jwtVerify(hint, createLocalJWKSet(issuer.jwks), { issuer: publicIssuer, audience: clientId, algorithms: ["RS256"] });
      return true;
    } catch {
      return false;
    }
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://issuer");
    if (!url.pathname.startsWith(path)) return json(res, 404, { error: "not_found" });
    const route = url.pathname.slice(path.length);

    if (route === "/.well-known/openid-configuration") {
      return json(res, 200, {
        issuer: publicIssuer,
        authorization_endpoint: `${publicIssuer}/protocol/openid-connect/auth`,
        token_endpoint: `${publicIssuer}/protocol/openid-connect/token`,
        userinfo_endpoint: `${publicIssuer}/protocol/openid-connect/userinfo`,
        jwks_uri: `${publicIssuer}/protocol/openid-connect/certs`,
        response_types_supported: ["code"],
        subject_types_supported: ["public"],
        id_token_signing_alg_values_supported: ["RS256"],
        code_challenge_methods_supported: ["S256"],
        token_endpoint_auth_methods_supported: ["client_secret_basic", "client_secret_post"],
        grant_types_supported: ["authorization_code", "refresh_token"],
        ...(issuer.endSession ? { end_session_endpoint: `${publicIssuer}/protocol/openid-connect/logout` } : {}),
      });
    }
    if (route === "/protocol/openid-connect/certs") return json(res, 200, issuer.jwks);
    if (route === "/protocol/openid-connect/userinfo") return json(res, 200, { sub: "sub-userinfo" });

    if (route === "/protocol/openid-connect/logout") {
      if (!issuer.endSession) return json(res, 404, { error: "not_found" });
      const hint = url.searchParams.get("id_token_hint") ?? undefined;
      const back = url.searchParams.get("post_logout_redirect_uri") ?? "";
      // Only the app's origin (`appOrigin`) or that of a redirect_uri used at sign-in may be returned to.
      const ok = back !== "" && URL.canParse(back) && redirectOrigins.has(new URL(back).origin);
      issuer.endSessionRequests.push({
        idTokenHint: hint,
        idTokenHintValid: hint === undefined ? false : await ownIdToken(hint),
        postLogoutRedirectUri: back,
        clientId: url.searchParams.get("client_id") ?? undefined,
        ok,
      });
      if (!ok) return json(res, 400, { error: "invalid_redirect_uri" });
      res.writeHead(302, { location: back });
      return res.end();
    }

    if (route === "/protocol/openid-connect/auth") {
      const q = Object.fromEntries(url.searchParams);
      issuer.authorizeRequests.push(q);
      if (q.client_id !== clientId || q.response_type !== "code" || !q.state) return json(res, 400, { error: "invalid_request" });
      if (URL.canParse(q.redirect_uri ?? "")) redirectOrigins.add(new URL(q.redirect_uri).origin);
      const code = crypto.randomUUID();
      codes.set(code, {
        nonce: q.nonce,
        challenge: q.code_challenge,
        method: q.code_challenge_method,
        redirectUri: q.redirect_uri,
        email: q.login_hint || defaultEmail,
      });
      const back = new URL(q.redirect_uri);
      back.searchParams.set("code", code);
      back.searchParams.set("state", q.state);
      res.writeHead(302, { location: back.href });
      return res.end();
    }

    if (route === "/protocol/openid-connect/token" && req.method === "POST") {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      const form = new URLSearchParams(Buffer.concat(chunks).toString());
      const basic = (req.headers.authorization ?? "").replace(/^Basic /, "");
      const [id, secret] = Buffer.from(basic, "base64").toString().split(":").map(decodeURIComponent);
      const authOk =
        (id === clientId && secret === clientSecret) || (form.get("client_id") === clientId && form.get("client_secret") === clientSecret);
      const grant = form.get("grant_type");
      if (!authOk) {
        issuer.tokenRequests.push({ grant_type: grant, ok: false, why: "client_auth" });
        return json(res, 401, { error: "invalid_client" });
      }
      if (grant === "authorization_code") {
        const rec = codes.get(form.get("code"));
        codes.delete(form.get("code"));
        const verifier = form.get("code_verifier") ?? "";
        const pkceOk = rec?.method === "S256" && b64url(crypto.createHash("sha256").update(verifier).digest()) === rec.challenge;
        if (!rec || !pkceOk || rec.redirectUri !== form.get("redirect_uri")) {
          issuer.tokenRequests.push({ grant_type: grant, ok: false, why: !pkceOk ? "pkce" : "code" });
          return json(res, 400, { error: "invalid_grant" });
        }
        issuer.tokenRequests.push({ grant_type: grant, ok: true });
        return json(res, 200, await tokens(rec.email, rec.nonce));
      }
      if (grant === "refresh_token") {
        await new Promise((r) => setTimeout(r, issuer.refreshDelayMs));
        const rt = form.get("refresh_token");
        const email = liveRefresh.get(rt);
        const valid = issuer.refreshMode === "ok" && email !== undefined && liveRefresh.delete(rt); // rotation: one use only
        issuer.tokenRequests.push({ grant_type: grant, ok: valid });
        if (!valid) return json(res, 400, { error: "invalid_grant" });
        return json(res, 200, await tokens(email)); // same user, a fresh id_token (as Keycloak)
      }
      return json(res, 400, { error: "unsupported_grant_type" });
    }
    return json(res, 404, { error: "not_found" });
  });

  await listen(server, port);
  return Object.assign(issuer, { close: () => close(server) });
}
