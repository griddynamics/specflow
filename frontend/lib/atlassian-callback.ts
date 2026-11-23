// docs/jira-sync/oauth.md#connect-flow: where the Atlassian consent callback sends the browser. Pure (no Next.js, no env), so the
// rules are tested without a server; app/api/integrations/atlassian/callback/route.ts wires them to the session and the API.
// Never logs and never echoes `code` or `state`: only the path of `return_to` and one fixed code reach the redirect.

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

/** Where the callback goes without a session: sign-in (it has no return target, so the user starts the connect again afterwards). */
export const SIGN_IN_PATH = "/login";

/** A same-site path: starts with `/`, not `//`, no backslash, no whitespace or control character, ASCII only; at most 2048 characters. */
export function isSameSitePath(r: unknown): r is string {
  if (typeof r !== "string" || r.length === 0 || r.length > 2048) return false;
  if (!r.startsWith("/") || r.startsWith("//") || r.includes("\\")) return false;
  for (const ch of r) {
    const c = ch.codePointAt(0) ?? 0;
    if (c <= 0x20 || c >= 0x7f) return false;
  }
  return true;
}

/** base64url to text; null when the input is not base64url or not UTF-8. */
function fromBase64Url(text: string): string | null {
  if (!/^[\w-]*$/.test(text)) return null;
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.from(text, "base64url"));
  } catch {
    return null;
  }
}

/**
 * The `return_to` inside `state` (`nonce.payload.signature`, payload base64url JSON `{"u","r","e"}`), or `/` when it is missing or not a
 * same-site path. The signature is the API's to check: this only decides where the browser lands after a refusal.
 */
export function returnToOf(state: string | null): string {
  const parts = state?.split(".") ?? [];
  if (parts.length !== 3) return "/";
  const raw = fromBase64Url(parts[1]);
  if (raw === null) return "/";
  try {
    const payload: unknown = JSON.parse(raw);
    const r = typeof payload === "object" && payload !== null ? (payload as { r?: unknown }).r : undefined;
    return isSameSitePath(r) ? r : "/";
  } catch {
    return "/";
  }
}

/** `path` with `?atlassian=<code>` set (other query parameters kept, a fragment dropped). */
export function withCode(path: string, code: AtlassianCode): string {
  const url = new URL(path, "https://same.site");
  url.searchParams.set("atlassian", code);
  return `${url.pathname}${url.search}`;
}

/** The code of an API error answer: a known route code, else `atlassian_unexpected`. */
export function codeOfAnswer(body: unknown): AtlassianCode {
  const error = typeof body === "object" && body !== null ? (body as { error?: unknown }).error : undefined;
  const code = typeof error === "object" && error !== null ? (error as { code?: unknown }).code : undefined;
  const known = ATLASSIAN_CODES.find((c) => c === code && c !== "connected" && c !== "atlassian_consent_denied");
  return known ?? "atlassian_unexpected";
}

/** What the API answered to `POST /integrations/atlassian/callback`: its status and parsed JSON body (null when none). */
export interface CallbackAnswer {
  status: number;
  body: unknown;
}

/**
 * The path the callback redirects to. `signedIn` false goes to sign-in. Atlassian's own `?error=` never reaches the API. A missing `code`
 * or `state` is `atlassian_unexpected`. A 401 from the API (the session's token was refused) goes to sign-in. `post` rejecting (the API
 * is down) is `atlassian_unexpected`.
 */
export async function callbackDestination(
  query: URLSearchParams,
  signedIn: boolean,
  post: (code: string, state: string) => Promise<CallbackAnswer>,
): Promise<string> {
  if (!signedIn) return SIGN_IN_PATH;
  const state = query.get("state");
  const back = returnToOf(state);
  if (query.has("error")) return withCode(back, "atlassian_consent_denied");
  const code = query.get("code");
  if (code === null || code === "" || state === null || state === "") return withCode(back, "atlassian_unexpected");
  let answer: CallbackAnswer;
  try {
    answer = await post(code, state);
  } catch {
    return withCode(back, "atlassian_unexpected");
  }
  if (answer.status === 401) return SIGN_IN_PATH;
  if (answer.status < 200 || answer.status >= 300) return withCode(back, codeOfAnswer(answer.body));
  const returnTo = typeof answer.body === "object" && answer.body !== null ? (answer.body as { return_to?: unknown }).return_to : undefined;
  return withCode(isSameSitePath(returnTo) ? returnTo : back, "connected");
}
