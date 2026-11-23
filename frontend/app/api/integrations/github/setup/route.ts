import { setupDestination } from "@/lib/github-callback";
import { oauthReturnRoute } from "@/lib/oauth-route";

// plans/GITHUB-INTEGRATION/contract.md#web-app-routes: GitHub's "Setup URL" ({PUBLIC_URL}/api/integrations/github/setup), reached after the
// user installs the Specflow GitHub App or changes its repositories. Only the signed `state` goes to the API; GitHub's `installation_id`
// is never read (it can be spoofed). The browser lands on `return_to` with `?github=installed` or `?github=<code>` (lib/github-callback.ts).
export const dynamic = "force-dynamic";

export const GET = oauthReturnRoute((query, signedIn, post) =>
  setupDestination(query, signedIn, (state) => post("/api/v1/integrations/github/setup", { state })),
);
