// docs/jira-sync/oauth.md#connect-flow: where the Atlassian consent callback sends the browser. The rules are the shared ones of
// lib/oauth-callback.ts with the `?atlassian=` codes; app/api/integrations/atlassian/callback/route.ts wires them to the session and the API.
import { codeCallbackDestination, withParam, type CallbackAnswer, type ReturnSpec } from "@/lib/oauth-callback";

/** The `?atlassian=` codes the Jira tab knows; any other API answer becomes `atlassian_unexpected`. */
export const ATLASSIAN_CODES = [
  "connected",
  "atlassian_consent_denied",
  "atlassian_state_invalid",
  "atlassian_code_invalid",
  "atlassian_site_not_granted",
  "atlassian_unreachable",
  "atlassian_oauth_unavailable",
  "atlassian_unexpected",
] as const;
export type AtlassianCode = (typeof ATLASSIAN_CODES)[number];

const API_CODES = ATLASSIAN_CODES.filter((c) => c !== "connected" && c !== "atlassian_consent_denied");

const SPEC: ReturnSpec<AtlassianCode> = {
  param: "atlassian",
  success: "connected",
  denied: "atlassian_consent_denied",
  unexpected: "atlassian_unexpected",
  apiCodes: API_CODES,
};

/** `path` with `?atlassian=<code>` set (other query parameters kept, a fragment dropped); `/` when the result is not a same-site path. */
export const withCode = (path: string, code: AtlassianCode): string => withParam(path, "atlassian", code);

/**
 * The path the callback redirects to. `signedIn` false goes to sign-in. Atlassian's own `?error=` never reaches the API. A missing `code`
 * or `state` is `atlassian_unexpected`. A 401 from the API (the session's token was refused) goes to sign-in. `post` rejecting (the API
 * is down) is `atlassian_unexpected`.
 */
export const callbackDestination = (
  query: URLSearchParams,
  signedIn: boolean,
  post: (code: string, state: string) => Promise<CallbackAnswer>,
): Promise<string> => codeCallbackDestination(SPEC, query, signedIn, post);
