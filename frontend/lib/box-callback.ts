// docs/box/oauth.md "Connect flow": where the Box return route sends the browser. The OAuth callback (app/api/integrations/box/callback) is the
// shared code flow of lib/oauth-callback.ts with the `?box=` codes (as lib/github-callback.ts, without an install step).
import { codeCallbackDestination, type CallbackAnswer, type ReturnSpec } from "@/lib/oauth-callback";

/** The `?box=` codes the Integrations page knows; any other API answer becomes `box_unexpected`. */
export const BOX_CODES = [
  "connected",
  "box_consent_denied",
  "box_state_invalid",
  "box_code_invalid",
  "box_unreachable",
  "box_auth_failed",
  "box_unavailable",
  "box_unexpected",
] as const;
export type BoxCode = (typeof BOX_CODES)[number];

const API_CODES: readonly BoxCode[] = ["box_state_invalid", "box_code_invalid", "box_unreachable", "box_auth_failed", "box_unavailable"];

const SPEC: ReturnSpec<BoxCode> = {
  param: "box",
  success: "connected",
  denied: "box_consent_denied",
  unexpected: "box_unexpected",
  apiCodes: API_CODES,
};

/** The OAuth callback: like GitHub's, with `?box=` (Box's own `?error=access_denied` is `box_consent_denied`). */
export const callbackDestination = (
  query: URLSearchParams,
  signedIn: boolean,
  post: (code: string, state: string) => Promise<CallbackAnswer>,
): Promise<string> => codeCallbackDestination(SPEC, query, signedIn, post);
