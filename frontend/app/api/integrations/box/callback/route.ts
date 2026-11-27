import { callbackDestination } from "@/lib/box-callback";
import { oauthReturnRoute } from "@/lib/oauth-route";

// docs/box/oauth.md "Connect flow": Box sends the browser here after the user grants access to the Graphico app
// ({PUBLIC_URL}/api/integrations/box/callback). The signed-in user's `code` and `state` go to the API on the server, and the browser lands
// on `return_to` with `?box=connected` or `?box=<code>` (lib/box-callback.ts).
export const dynamic = "force-dynamic";

export const GET = oauthReturnRoute((query, signedIn, post) =>
  callbackDestination(query, signedIn, (code, state) => post("/api/v1/integrations/box/callback", { code, state })),
);
