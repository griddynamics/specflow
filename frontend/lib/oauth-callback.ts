// The rules every OAuth return route of the web app shares (Atlassian: lib/atlassian-callback.ts; GitHub: lib/github-callback.ts). Pure
// (no Next.js, no env), so they are tested without a server. Never logs and never echoes `code` or `state`: only the path of `return_to`
// and one fixed code reach the redirect.

import { isSameSitePath } from "./same-site-path";

/** Where a return route goes without a session: sign-in (it has no return target, so the user starts the connect again afterwards). */
export const SIGN_IN_PATH = "/login";

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
 * The `return_to` inside `state` (`nonce.payload.signature`, payload base64url JSON with `r`), or `/` when it is missing or not a
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

/** `path` with `?{param}=<code>` set (other query parameters kept, a fragment dropped); `/?{param}=<code>` when not a same-site path. */
export function withParam(path: string, param: string, code: string): string {
  const url = new URL(path, "https://same.site");
  url.searchParams.set(param, code);
  const result = `${url.pathname}${url.search}`;
  return url.origin === "https://same.site" && isSameSitePath(result) ? result : `/?${param}=${code}`;
}

/** The absolute redirect for `path` on `publicOrigin`; `{publicOrigin}/` when `path` would resolve to another origin. */
export function locationOn(path: string, publicOrigin: string): string {
  const origin = new URL(publicOrigin).origin;
  try {
    const location = new URL(path, origin);
    if (location.origin === origin) return location.href;
  } catch {
    // not a URL: the fallback below
  }
  return `${origin}/`;
}

/** The `error.code` of an API error answer when it is one of `known`, else `fallback`. */
export function codeOfAnswer<C extends string>(body: unknown, known: readonly C[], fallback: C): C {
  const error = typeof body === "object" && body !== null ? (body as { error?: unknown }).error : undefined;
  const code = typeof error === "object" && error !== null ? (error as { code?: unknown }).code : undefined;
  return known.find((c) => c === code) ?? fallback;
}

/** What the API answered to a return route's POST: its status and parsed JSON body (null when none). */
export interface CallbackAnswer {
  status: number;
  body: unknown;
}

/** The `return_to` of a success answer, when it is a same-site path. */
export function answerReturnTo(body: unknown): string | null {
  const r = typeof body === "object" && body !== null ? (body as { return_to?: unknown }).return_to : undefined;
  return isSameSitePath(r) ? r : null;
}

/** One OAuth return: its `?{param}=` codes (the success one, the provider's refusal, the fallback) and the API codes it passes on. */
export interface ReturnSpec<C extends string> {
  param: string;
  success: C;
  denied: C;
  unexpected: C;
  /** API error codes shown as they are; any other refusal is `unexpected`. */
  apiCodes: readonly C[];
}

/**
 * The path an OAuth code callback redirects to. `signedIn` false goes to sign-in. The provider's own `?error=` never reaches the API.
 * A missing `code` or `state` is `unexpected`. A 401 from the API (the session's token was refused) goes to sign-in. `post` rejecting
 * (the API is down) is `unexpected`.
 */
export async function codeCallbackDestination<C extends string>(
  spec: ReturnSpec<C>,
  query: URLSearchParams,
  signedIn: boolean,
  post: (code: string, state: string) => Promise<CallbackAnswer>,
): Promise<string> {
  if (!signedIn) return SIGN_IN_PATH;
  const state = query.get("state");
  const back = returnToOf(state);
  if (query.has("error")) return withParam(back, spec.param, spec.denied);
  const code = query.get("code");
  if (code === null || code === "" || state === null || state === "") return withParam(back, spec.param, spec.unexpected);
  let answer: CallbackAnswer;
  try {
    answer = await post(code, state);
  } catch {
    return withParam(back, spec.param, spec.unexpected);
  }
  if (answer.status === 401) return SIGN_IN_PATH;
  if (answer.status < 200 || answer.status >= 300) {
    return withParam(back, spec.param, codeOfAnswer(answer.body, spec.apiCodes, spec.unexpected));
  }
  return withParam(answerReturnTo(answer.body) ?? back, spec.param, spec.success);
}
