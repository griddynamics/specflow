import { callbackDestination } from "@/lib/dropbox-callback";
import { oauthReturnRoute } from "@/lib/oauth-route";

// docs/dropbox/oauth.md "Connect flow": Dropbox sends the browser here after the user grants access to the Graphico app
// ({PUBLIC_URL}/api/integrations/dropbox/callback). The signed-in user's `code` and `state` go to the API on the server, and the browser lands
// on `return_to` with `?dropbox=connected` or `?dropbox=<code>` (lib/dropbox-callback.ts).
export const dynamic = "force-dynamic";

export const GET = oauthReturnRoute((query, signedIn, post) =>
  callbackDestination(query, signedIn, (code, state) => post("/api/v1/integrations/dropbox/callback", { code, state })),
);
