// docs/dropbox/oauth.md "Connect flow": where the Dropbox return route sends the browser (lib/dropbox-callback.ts), with the
// cases every code-flow OAuth app shares (tests/support/oauth-return-suite.ts). The route's wiring is `oauthReturnRoute`.
import { DROPBOX_CODES, callbackDestination } from "@/lib/dropbox-callback";
import { test } from "node:test";
import { oauthReturnCases } from "../support/oauth-return-suite";

for (const [name, run] of oauthReturnCases("dropbox", DROPBOX_CODES, callbackDestination)) test(name, run);
