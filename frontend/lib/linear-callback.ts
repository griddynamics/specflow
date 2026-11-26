// plans/LINEAR/LINEAR-PLAN.md, stream S1: where the Linear consent callback sends the browser. The rules are the shared ones of
// lib/oauth-callback.ts with the `?linear=` codes; app/api/integrations/linear/callback/route.ts wires them to the session and the API.
import { codeCallbackDestination, type CallbackAnswer, type ReturnSpec } from "@/lib/oauth-callback";

/** The `?linear=` codes the Integrations page knows; any other API answer becomes `linear_unexpected`. */
export const LINEAR_CODES = [
  "connected",
  "linear_consent_denied",
  "linear_state_invalid",
  "linear_code_invalid",
  "linear_unreachable",
  "linear_auth_failed",
  "linear_unavailable",
  "linear_unexpected",
] as const;
export type LinearCode = (typeof LINEAR_CODES)[number];

const API_CODES = LINEAR_CODES.filter((c) => c !== "connected" && c !== "linear_consent_denied");

const SPEC: ReturnSpec<LinearCode> = {
  param: "linear",
  success: "connected",
  denied: "linear_consent_denied",
  unexpected: "linear_unexpected",
  apiCodes: API_CODES,
};

/** The OAuth callback: like Atlassian's, with `?linear=` (Linear's own `?error=` is `linear_consent_denied`). */
export const callbackDestination = (
  query: URLSearchParams,
  signedIn: boolean,
  post: (code: string, state: string) => Promise<CallbackAnswer>,
): Promise<string> => codeCallbackDestination(SPEC, query, signedIn, post);
