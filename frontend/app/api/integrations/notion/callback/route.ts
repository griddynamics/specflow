import { callbackDestination } from "@/lib/notion-callback";
import { oauthReturnRoute } from "@/lib/oauth-route";

// docs/notion/oauth.md#connect-flow: Notion sends the browser here after the consent and the page picker
// ({PUBLIC_URL}/api/integrations/notion/callback). The signed-in user's `code` and `state` go to the API on the server, and the browser lands
// on `return_to` with `?notion=connected` or `?notion=<code>` (lib/notion-callback.ts).
export const dynamic = "force-dynamic";

export const GET = oauthReturnRoute((query, signedIn, post) =>
  callbackDestination(query, signedIn, (code, state) => post("/api/v1/integrations/notion/callback", { code, state })),
);
