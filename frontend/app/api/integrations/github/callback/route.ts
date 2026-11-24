import { callbackDestination } from "@/lib/github-callback";
import { oauthReturnRoute } from "@/lib/oauth-route";

// docs/github/oauth.md#connect-flow: GitHub sends the browser here after the user authorizes the Specflow GitHub App
// ({PUBLIC_URL}/api/integrations/github/callback). The signed-in user's `code` and `state` go to the API on the server, and the browser lands
// on `return_to` with `?github=connected` or `?github=<code>` (lib/github-callback.ts).
export const dynamic = "force-dynamic";

export const GET = oauthReturnRoute((query, signedIn, post) =>
  callbackDestination(query, signedIn, (code, state) => post("/api/v1/integrations/github/callback", { code, state })),
);
