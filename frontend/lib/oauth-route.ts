import { auth } from "@/auth";
import { appConfig } from "@/lib/config";
import { type CallbackAnswer } from "@/lib/oauth-callback";
import { redirectOn, runGuarded } from "@/lib/redirect-route";
import { accessTokenFor } from "@/lib/session-token";

// The GET handler of an OAuth return route (app/api/integrations/{atlassian,github}/...): the signed-in user's values go to the API on the
// server with the user's access token (as the BFF sends it), and the browser is redirected where `destination` decides (lib/oauth-callback.ts
// and its provider wrappers). Static segments, so neither the BFF nor the Auth.js catch-all serves them; the page proxy (proxy.ts) skips
// `/api`, so the session is checked here. Never logged, never cached.

const API_TIMEOUT_MS = 15_000;

/** POSTs `body` as JSON to the API path `apiPath` (`/api/v1/...`) with the user's bearer token; the answer's status and parsed body. */
async function postToApi(apiInternalUrl: string, token: string, apiPath: string, body: unknown): Promise<CallbackAnswer> {
  const res = await fetch(new URL(apiPath, apiInternalUrl), {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
    redirect: "manual",
    signal: AbortSignal.timeout(API_TIMEOUT_MS),
  });
  const text = await res.text();
  let parsed: unknown = null;
  try {
    parsed = text === "" ? null : JSON.parse(text);
  } catch {
    parsed = null;
  }
  return { status: res.status, body: parsed };
}

/** Sends `body` to `apiPath` for the signed-in user of the route. */
export type ApiPost = (apiPath: string, body: unknown) => Promise<CallbackAnswer>;

/** A GET route handler that redirects (302, absolute on PUBLIC_URL, never another origin) to what `destination` returns. */
export function oauthReturnRoute(destination: (query: URLSearchParams, signedIn: boolean, post: ApiPost) => Promise<string>) {
  const guarded = auth(async (req) => {
    const cfg = appConfig();
    const token = req.auth ? await accessTokenFor(req) : null;
    const signedIn = token !== null && token !== "";
    const path = await destination(new URL(req.url).searchParams, signedIn, (apiPath, body) =>
      postToApi(cfg.apiInternalUrl, token ?? "", apiPath, body),
    );
    return redirectOn(path, cfg.publicOrigin);
  });

  type Req = Parameters<typeof guarded>[0];
  type Ctx = Parameters<typeof guarded>[1];

  return async function GET(req: Req, ctx: Ctx): Promise<Response> {
    return runGuarded(guarded, req, ctx);
  };
}
