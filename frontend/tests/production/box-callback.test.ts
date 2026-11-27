// docs/box/oauth.md "Connect flow": where the Box return route sends the browser (lib/box-callback.ts), with the cases every
// code-flow OAuth app shares (tests/support/oauth-return-suite.ts). The route's wiring is `oauthReturnRoute`.
import { BOX_CODES, callbackDestination } from "@/lib/box-callback";
import { test } from "node:test";
import { oauthReturnCases } from "../support/oauth-return-suite";

for (const [name, run] of oauthReturnCases("box", BOX_CODES, callbackDestination)) test(name, run);
