import { callbackDestination } from "@/lib/linear-callback";
import { oauthReturnRoute } from "@/lib/oauth-route";

// plans/LINEAR/LINEAR-PLAN.md: Linear sends the browser here after the member approves the Graphico app ({PUBLIC_URL}/api/integrations/linear/callback).
// The signed-in user's `code` and `state` go to the API on the server, and the browser lands on `return_to` with `?linear=connected` or
// `?linear=<code>` (lib/linear-callback.ts).
export const dynamic = "force-dynamic";

export const GET = oauthReturnRoute((query, signedIn, post) =>
  callbackDestination(query, signedIn, (code, state) => post("/api/v1/integrations/linear/callback", { code, state })),
);
