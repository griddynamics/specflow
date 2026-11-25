// docs/notion/oauth.md#connect-flow: where the Notion OAuth return route sends the browser. The shared code flow of lib/oauth-callback.ts with
// the `?notion=` codes (Notion's own `?error=` is `notion_consent_denied`). Notion has no install step, so there is no setup route.
import { codeCallbackDestination, type CallbackAnswer, type ReturnSpec } from "@/lib/oauth-callback";

/** The `?notion=` codes the Integrations page knows; any other API answer becomes `notion_unexpected`. */
export const NOTION_CODES = [
  "connected",
  "notion_consent_denied",
  "notion_state_invalid",
  "notion_code_invalid",
  "notion_unreachable",
  "notion_auth_failed",
  "notion_unavailable",
  "notion_unexpected",
] as const;
export type NotionCode = (typeof NOTION_CODES)[number];

const API_CODES: readonly NotionCode[] = [
  "notion_state_invalid",
  "notion_code_invalid",
  "notion_unreachable",
  "notion_auth_failed",
  "notion_unavailable",
];

const SPEC: ReturnSpec<NotionCode> = {
  param: "notion",
  success: "connected",
  denied: "notion_consent_denied",
  unexpected: "notion_unexpected",
  apiCodes: API_CODES,
};

/** The OAuth callback: `code` and `state` go to the API once; the browser lands on the API's `return_to` with `?notion=connected` or `?notion=<code>`. */
export const callbackDestination = (
  query: URLSearchParams,
  signedIn: boolean,
  post: (code: string, state: string) => Promise<CallbackAnswer>,
): Promise<string> => codeCallbackDestination(SPEC, query, signedIn, post);
