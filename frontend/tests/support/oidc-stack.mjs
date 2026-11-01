// The identity side of a harness run: the stub issuer and the stub API that verifies its tokens, in this process, plus the
// app env that points the built frontend at them. The issuer's public URL is its own address, so a test (or a browser)
// reaches it directly and the app needs no OIDC_INTERNAL_URL. Keys and secrets are generated here at start.
import { randomSecret } from "./servers.mjs";
import { startStubApi } from "./stub-api.mjs";
import { startStubIssuer } from "../oidc/stub-issuer.mjs";

export const OIDC_CLIENT_ID = "specflow-web";
export const API_AUDIENCE = "specflow-api";

/**
 * @param {{issuerPort:number, apiPort?:number, defaultEmail?:string, api?:object}} o `api` is passed on to startStubApi; without
 * `apiPort` no stub API starts (the test brings its own upstream and sets API_INTERNAL_URL).
 * @returns `env` (add it to frontendEnv), `secrets` (values a child's output must never show) and `close()`.
 */
export async function startOidcStack({ issuerPort, apiPort, defaultEmail, api: apiOptions = {} }) {
  const clientSecret = randomSecret();
  const sessionSecret = randomSecret();
  const publicIssuer = `http://127.0.0.1:${issuerPort}/realms/stub`;
  const issuer = await startStubIssuer({
    port: issuerPort,
    publicIssuer,
    clientId: OIDC_CLIENT_ID,
    clientSecret,
    audience: API_AUDIENCE,
    defaultEmail,
  });
  const api =
    apiPort === undefined
      ? undefined
      : await startStubApi({ port: apiPort, verify: { issuer: publicIssuer, audience: API_AUDIENCE, jwks: issuer.jwks }, ...apiOptions });
  return {
    issuer,
    api,
    env: {
      ...(api ? { API_INTERNAL_URL: api.url } : {}),
      SESSION_SECRET: sessionSecret,
      OIDC_ISSUER: publicIssuer,
      OIDC_CLIENT_ID,
      OIDC_CLIENT_SECRET: clientSecret,
    },
    secrets: [clientSecret, sessionSecret],
    async close() {
      await api?.close();
      await issuer.close();
    },
  };
}
