// plans/GITHUB-INTEGRATION/contract.md#web-app-routes: where the two GitHub return routes send the browser. The OAuth callback
// (app/api/integrations/github/callback) is the shared code flow of lib/oauth-callback.ts with the `?github=` codes; the setup route
// (app/api/integrations/github/setup, after an app install or an update of its repositories) only has the app's signed `state` checked by
// the API. GitHub's `installation_id` is never read or forwarded: the API lists installations through the user's token.
import {
  codeCallbackDestination,
  codeOfAnswer,
  answerReturnTo,
  returnToOf,
  SIGN_IN_PATH,
  withParam,
  type CallbackAnswer,
  type ReturnSpec,
} from "@/lib/oauth-callback";

/** The `?github=` codes the GitHub tab knows; any other API answer becomes `github_unexpected`. */
export const GITHUB_CODES = [
  "connected",
  "installed",
  "github_consent_denied",
  "github_state_invalid",
  "github_code_invalid",
  "github_unreachable",
  "github_auth_failed",
  "github_unavailable",
  "github_unexpected",
] as const;
export type GitHubCode = (typeof GITHUB_CODES)[number];

const API_CODES: readonly GitHubCode[] = [
  "github_state_invalid",
  "github_code_invalid",
  "github_unreachable",
  "github_auth_failed",
  "github_unavailable",
];

const SPEC: ReturnSpec<GitHubCode> = {
  param: "github",
  success: "connected",
  denied: "github_consent_denied",
  unexpected: "github_unexpected",
  apiCodes: API_CODES,
};

/** The OAuth callback: like Atlassian's, with `?github=` (GitHub's own `?error=` is `github_consent_denied`). */
export const callbackDestination = (
  query: URLSearchParams,
  signedIn: boolean,
  post: (code: string, state: string) => Promise<CallbackAnswer>,
): Promise<string> => codeCallbackDestination(SPEC, query, signedIn, post);

/**
 * The setup route (GitHub's "Setup URL" after an install or a change of its repositories). No session goes to sign-in. Without `state`
 * (installed from GitHub's own pages, not from Specflow) the browser goes home and the API is not called. Otherwise the API checks the
 * signed state: success lands on its `return_to` with `?github=installed`, a refusal on the state's `return_to` with its code.
 */
export async function setupDestination(
  query: URLSearchParams,
  signedIn: boolean,
  post: (state: string) => Promise<CallbackAnswer>,
): Promise<string> {
  if (!signedIn) return SIGN_IN_PATH;
  const state = query.get("state");
  if (state === null || state === "") return "/";
  const back = returnToOf(state);
  let answer: CallbackAnswer;
  try {
    answer = await post(state);
  } catch {
    return withParam(back, "github", "github_unexpected");
  }
  if (answer.status === 401) return SIGN_IN_PATH;
  if (answer.status < 200 || answer.status >= 300) {
    return withParam(back, "github", codeOfAnswer(answer.body, API_CODES, "github_unexpected"));
  }
  return withParam(answerReturnTo(answer.body) ?? back, "github", "installed");
}
