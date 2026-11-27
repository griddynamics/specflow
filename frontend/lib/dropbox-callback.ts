// docs/dropbox/oauth.md "Connect flow": where the Dropbox return route sends the browser. The OAuth callback (app/api/integrations/dropbox/callback) is the
// shared code flow of lib/oauth-callback.ts with the `?dropbox=` codes (as lib/github-callback.ts, without an install step).
import { codeCallbackDestination, type CallbackAnswer, type ReturnSpec } from "@/lib/oauth-callback";

/** The `?dropbox=` codes the Integrations page knows; any other API answer becomes `dropbox_unexpected`. */
export const DROPBOX_CODES = [
  "connected",
  "dropbox_consent_denied",
  "dropbox_state_invalid",
  "dropbox_code_invalid",
  "dropbox_unreachable",
  "dropbox_auth_failed",
  "dropbox_unavailable",
  "dropbox_unexpected",
] as const;
export type DropboxCode = (typeof DROPBOX_CODES)[number];

const API_CODES: readonly DropboxCode[] = [
  "dropbox_state_invalid",
  "dropbox_code_invalid",
  "dropbox_unreachable",
  "dropbox_auth_failed",
  "dropbox_unavailable",
];

const SPEC: ReturnSpec<DropboxCode> = {
  param: "dropbox",
  success: "connected",
  denied: "dropbox_consent_denied",
  unexpected: "dropbox_unexpected",
  apiCodes: API_CODES,
};

/** The OAuth callback: like GitHub's, with `?dropbox=` (Dropbox's own `?error=access_denied` is `dropbox_consent_denied`). */
export const callbackDestination = (
  query: URLSearchParams,
  signedIn: boolean,
  post: (code: string, state: string) => Promise<CallbackAnswer>,
): Promise<string> => codeCallbackDestination(SPEC, query, signedIn, post);
