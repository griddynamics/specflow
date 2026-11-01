import { appConfig } from "./config";
import { endSessionEndpoint } from "./oidc-refresh";
import { idTokenFor } from "./session-token";

const SIGNED_OUT_PATH = "/login?signedout=1";

/**
 * Server-only: where the browser goes to finish a sign-out. With an `end_session_endpoint` advertised by the issuer and an id_token in
 * the session cookie of `req`, the issuer's logout URL (RP-initiated logout); otherwise the app's own signed-out page, so the sign-out
 * is local only (Google has no such endpoint). The return address is built from PUBLIC_URL, never from the request. The id_token
 * appears only inside this URL.
 */
export async function endSessionUrlFor(req: Request): Promise<string> {
  const { publicOrigin, oidc } = appConfig();
  const fallback = new URL(SIGNED_OUT_PATH, publicOrigin).href;
  try {
    const [endpoint, idToken] = await Promise.all([endSessionEndpoint(), idTokenFor(req)]);
    if (endpoint === undefined || idToken === null || !URL.canParse(endpoint)) return fallback;
    const url = new URL(endpoint);
    if (url.protocol !== "https:" && url.protocol !== "http:") return fallback;
    url.searchParams.set("id_token_hint", idToken);
    url.searchParams.set("post_logout_redirect_uri", fallback);
    url.searchParams.set("client_id", oidc.clientId);
    return url.href;
  } catch {
    process.stdout.write(`${JSON.stringify({ event: "auth.signout.idp_unavailable" })}\n`);
    return fallback;
  }
}
