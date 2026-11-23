// The answer of the server route handlers a browser navigates to and that redirect it back into the app: the Atlassian consent callback
// (app/api/integrations/atlassian/callback/route.ts) and the Slack account link (app/api/integrations/slack/link/route.ts).
import { locationOn } from "@/lib/atlassian-callback";

/** A 302 to `path` on PUBLIC_URL, never another origin (`req.url` can carry the internal bind address 0.0.0.0); not cached, no referrer. */
export function redirectOn(path: string, publicOrigin: string): Response {
  return new Response(null, {
    status: 302,
    headers: { location: locationOn(path, publicOrigin), "cache-control": "no-store", "referrer-policy": "no-referrer" },
  });
}

/** Runs an `auth(fn)` handler, which is a Promise of the handler when the config is lazy (auth.ts, as in the BFF); no Response is a 500. */
export async function runGuarded<A extends unknown[]>(
  guarded: ((...args: A) => unknown) | Promise<(...args: A) => unknown>,
  ...args: A
): Promise<Response> {
  const response = await (await Promise.resolve(guarded))(...args);
  return response instanceof Response ? response : new Response(null, { status: 500 });
}
