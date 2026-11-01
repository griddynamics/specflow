import { getToken } from "next-auth/jwt";
import { appConfig, sessionCookieName } from "./config";
import { needsRefresh, refreshedFor } from "./oidc-refresh";

/** Server-only: the decoded session JWT of the cookie of `req`, or null. Only the Cookie header is read (never a Bearer header). */
async function sessionJwt(req: Request) {
  const cfg = appConfig();
  const cookieName = sessionCookieName(cfg.publicUrl);
  return getToken({
    req: { headers: new Headers({ cookie: req.headers.get("cookie") ?? "" }) },
    secret: cfg.sessionSecret,
    salt: cookieName,
    cookieName,
    secureCookie: cfg.secure,
  });
}

/** Server-only: the id_token kept in the session cookie of `req` (the RP-initiated logout hint), or null. */
export async function idTokenFor(req: Request): Promise<string | null> {
  const idToken = (await sessionJwt(req))?.id_token;
  return idToken === undefined || idToken === "" ? null : idToken;
}

/**
 * Server-only: the API access token for the session cookie of `req`. Call it after
 * `auth()` ran for the same request, which performs (single-flight) the refresh.
 * Tokens never leave the server (AC-6.7).
 */
export async function accessTokenFor(req: Request): Promise<string | null> {
  const jwt = await sessionJwt(req);
  const accessToken = jwt?.access_token;
  if (jwt === null || accessToken === undefined || accessToken === "") return null;
  if (!needsRefresh(jwt.expires_at)) return accessToken;
  const refreshToken = jwt.refresh_token;
  if (refreshToken === undefined || refreshToken === "") return null;
  const refreshed = await refreshedFor(refreshToken);
  return refreshed?.ok === true ? refreshed.tokens.access_token : null;
}
