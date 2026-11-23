import { auth } from "@/auth";
import { callbackDestination, locationOn, type CallbackAnswer } from "@/lib/atlassian-callback";
import { appConfig } from "@/lib/config";
import { accessTokenFor } from "@/lib/session-token";

// docs/jira-sync/oauth.md#connect-flow: Atlassian sends the browser here after the consent ({PUBLIC_URL}/api/integrations/atlassian/callback).
// The signed-in user's `code` and `state` go to the API on the server, with the user's access token (as the BFF sends it), and the browser
// lands on `return_to` with `?atlassian=connected` or `?atlassian=<code>` (lib/atlassian-callback.ts). A static segment, so neither the BFF
// nor the Auth.js catch-all serves it; the page proxy (proxy.ts) skips `/api`, so the session is checked here. Never logged, never cached.
export const dynamic = "force-dynamic";

const API_TIMEOUT_MS = 15_000;

async function postCallback(apiInternalUrl: string, token: string, code: string, state: string): Promise<CallbackAnswer> {
  const res = await fetch(new URL("/api/v1/integrations/atlassian/callback", apiInternalUrl), {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ code, state }),
    cache: "no-store",
    redirect: "manual",
    signal: AbortSignal.timeout(API_TIMEOUT_MS),
  });
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text === "" ? null : JSON.parse(text);
  } catch {
    body = null;
  }
  return { status: res.status, body };
}

const guarded = auth(async (req) => {
  const cfg = appConfig();
  const token = req.auth ? await accessTokenFor(req) : null;
  const signedIn = token !== null && token !== "";
  const path = await callbackDestination(new URL(req.url).searchParams, signedIn, (code, state) =>
    postCallback(cfg.apiInternalUrl, token ?? "", code, state),
  );
  // Absolute URL on PUBLIC_URL: req.url can carry the internal bind address (0.0.0.0). Never another origin (locationOn).
  return new Response(null, {
    status: 302,
    headers: { location: locationOn(path, cfg.publicOrigin), "cache-control": "no-store", "referrer-policy": "no-referrer" },
  });
});

// `auth(fn)` returns a Promise of the wrapped handler when the config is lazy (auth.ts), as in the BFF.
type Req = Parameters<typeof guarded>[0];
type Ctx = Parameters<typeof guarded>[1];

export async function GET(req: Req, ctx: Ctx): Promise<Response> {
  const response = await (await Promise.resolve(guarded))(req, ctx);
  return response instanceof Response ? response : new Response(null, { status: 500 });
}
