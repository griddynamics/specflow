import { getToken } from "next-auth/jwt";
import { appConfig, sessionCookieName } from "./config";
import { needsRefresh, refreshedFor } from "./oidc-refresh";

/**
 * Server-only: the API access token for the session cookie of `req`. Call it after
 * `auth()` ran for the same request, which performs (single-flight) the refresh.
 * Tokens never leave the server (AC-6.7).
 */
export async function accessTokenFor(req: Request): Promise<string | null> {
  const cfg = appConfig();
  const cookieName = sessionCookieName(cfg.publicUrl);
  // Only the Cookie header is passed, so getToken cannot fall back to a Bearer header.
  const jwt = await getToken({
    req: { headers: new Headers({ cookie: req.headers.get("cookie") ?? "" }) },
    secret: cfg.sessionSecret,
    salt: cookieName,
    cookieName,
    secureCookie: cfg.secure,
  });
  const accessToken = jwt?.access_token;
  if (jwt === null || accessToken === undefined || accessToken === "") return null;
  if (!needsRefresh(jwt.expires_at)) return accessToken;
  const refreshToken = jwt.refresh_token;
  if (cfg.authMode === "dev" || refreshToken === undefined || refreshToken === "") return null;
  const refreshed = await refreshedFor(refreshToken);
  return refreshed?.ok === true ? refreshed.tokens.access_token : null;
}
