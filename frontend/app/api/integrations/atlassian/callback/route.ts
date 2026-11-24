import { callbackDestination } from "@/lib/atlassian-callback";
import { oauthReturnRoute } from "@/lib/oauth-route";

// docs/jira-sync/oauth.md#connect-flow: Atlassian sends the browser here after the consent ({PUBLIC_URL}/api/integrations/atlassian/callback).
// The signed-in user's `code` and `state` go to the API on the server, and the browser lands on `return_to` with `?atlassian=connected` or
// `?atlassian=<code>` (lib/atlassian-callback.ts). The session, the token and the redirect are lib/oauth-route.ts.
export const dynamic = "force-dynamic";

export const GET = oauthReturnRoute((query, signedIn, post) =>
  callbackDestination(query, signedIn, (code, state) => post("/api/v1/integrations/atlassian/callback", { code, state })),
);
