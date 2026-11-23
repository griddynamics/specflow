import { auth } from "@/auth";
import { redirectOn, runGuarded } from "@/lib/redirect-route";
import { appConfig } from "@/lib/config";
import { accessTokenFor } from "@/lib/session-token";

// docs/slack/gap-answering.md "Parts": `/specflow link` in Slack gives a one-time link here ({PUBLIC_URL}/api/integrations/slack/link?nonce=).
// The signed-in user's nonce goes to the API on the server, with the user's access token (as the BFF sends it), which links the Slack
// user the nonce was made for to this user and confirms it in Slack. The browser lands on `/?slack=linked` or `/?slack=<code>`; without a
// session it goes to sign-in (the person runs `/specflow link` again). The page proxy (proxy.ts) skips `/api`, so the session is checked
// here. The nonce is never logged or echoed.
export const dynamic = "force-dynamic";

const API_TIMEOUT_MS = 15_000;
const SIGN_IN_PATH = "/login";

async function postLink(apiInternalUrl: string, token: string, nonce: string): Promise<string> {
  try {
    const res = await fetch(new URL("/api/v1/integrations/slack/link", apiInternalUrl), {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ nonce }),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
    if (res.status === 401) return SIGN_IN_PATH;
    if (res.ok) return "/?slack=linked";
    if (res.status === 400) return "/?slack=slack_link_invalid";
    return "/?slack=slack_unavailable";
  } catch {
    return "/?slack=slack_unavailable";
  }
}

const guarded = auth(async (req) => {
  const cfg = appConfig();
  const token = req.auth ? await accessTokenFor(req) : null;
  const nonce = new URL(req.url).searchParams.get("nonce");
  let path = SIGN_IN_PATH;
  if (token !== null && token !== "") {
    path = nonce === null || nonce === "" ? "/?slack=slack_link_invalid" : await postLink(cfg.apiInternalUrl, token, nonce);
  }
  return redirectOn(path, cfg.publicOrigin);
});

type Req = Parameters<typeof guarded>[0];
type Ctx = Parameters<typeof guarded>[1];

export async function GET(req: Req, ctx: Ctx): Promise<Response> {
  return runGuarded(guarded, req, ctx);
}
