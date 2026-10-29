// A small local OIDC issuer for the contract test (AC-6.9): discovery, JWKS, authorize
// (auto-approve, redirects back with code and state), token (authorization_code with
// PKCE S256, refresh_token with one-time rotation), userinfo. Test double only.
import http from "node:http";
import crypto from "node:crypto";
import { SignJWT, exportJWK, generateKeyPair } from "jose";
import { close, listen } from "../support/servers.mjs";

const b64url = (buf) => Buffer.from(buf).toString("base64url");
const json = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
};

/**
 * @param {{port:number, publicIssuer:string, clientId:string, clientSecret:string, path?:string}} o
 * `publicIssuer` is the issuer string put in tokens and discovery (what browsers see);
 * the server itself answers on `127.0.0.1:port` under `path`.
 */
export async function startStubIssuer({ port, publicIssuer, clientId, clientSecret, path = "/realms/stub" }) {
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  const jwk = { ...(await exportJWK(publicKey)), kid: "stub-key-1", alg: "RS256", use: "sig" };
  const codes = new Map(); // code -> {nonce, challenge, redirectUri, email}
  const liveRefresh = new Set();
  let counter = 0;

  const issuer = {
    url: `http://127.0.0.1:${port}${path}`,
    // controls (mutated by the test)
    accessExpiresIn: 3600,
    accessPadding: 0, // extra characters in the access token, to force a chunked session cookie
    refreshMode: "ok", // "ok" | "fail"
    refreshDelayMs: 250,
    // observations
    authorizeRequests: [],
    tokenRequests: [], // {grant_type, ok}
    get refreshCount() {
      return issuer.tokenRequests.filter((r) => r.grant_type === "refresh_token").length;
    },
    accessTokens: [],
  };

  async function tokens(email, nonce) {
    counter += 1;
    const access = `at-${counter}` + (issuer.accessPadding ? `-${"x".repeat(issuer.accessPadding)}` : "");
    const refresh = `rt-${counter}`;
    liveRefresh.add(refresh);
    issuer.accessTokens.push(access);
    const idToken = await new SignJWT({ email, email_verified: true, name: email.split("@")[0], ...(nonce ? { nonce } : {}) })
      .setProtectedHeader({ alg: "RS256", kid: jwk.kid })
      .setIssuer(publicIssuer)
      .setAudience(clientId)
      .setSubject(`sub-${email}`)
      .setIssuedAt()
      .setExpirationTime("5m")
      .sign(privateKey);
    return { access_token: access, refresh_token: refresh, id_token: idToken, token_type: "Bearer", expires_in: issuer.accessExpiresIn };
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
      });
    }
    if (route === "/protocol/openid-connect/certs") return json(res, 200, { keys: [jwk] });
    if (route === "/protocol/openid-connect/userinfo") return json(res, 200, { sub: "sub-userinfo" });

    if (route === "/protocol/openid-connect/auth") {
      const q = Object.fromEntries(url.searchParams);
      issuer.authorizeRequests.push(q);
      if (q.client_id !== clientId || q.response_type !== "code" || !q.state) return json(res, 400, { error: "invalid_request" });
      const code = crypto.randomUUID();
      codes.set(code, {
        nonce: q.nonce,
        challenge: q.code_challenge,
        method: q.code_challenge_method,
        redirectUri: q.redirect_uri,
        email: q.login_hint ?? "oidc.user@example.test",
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
        const valid = issuer.refreshMode === "ok" && liveRefresh.delete(rt); // rotation: one use only
        issuer.tokenRequests.push({ grant_type: grant, ok: valid });
        if (!valid) return json(res, 400, { error: "invalid_grant" });
        return json(res, 200, { ...(await tokens("oidc.user@example.test")), id_token: undefined });
      }
      return json(res, 400, { error: "unsupported_grant_type" });
    }
    return json(res, 404, { error: "not_found" });
  });

  await listen(server, port);
  return Object.assign(issuer, { close: () => close(server) });
}
