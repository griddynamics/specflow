import { appConfig } from "./config";

// AC-6.8: refresh is single-flight per session. The key is the refresh token
// carried by the session cookie. Parallel requests that present the same cookie
// share one in-flight refresh; a finished result (or failure) stays cached for a
// short time so requests still carrying the old cookie (server components cannot
// write cookies) do not spend an already rotated refresh token again.

export const REFRESH_LEEWAY_SECONDS = 30;
const RESULT_TTL_MS = 120_000;
const FAILURE_TTL_MS = 30_000;
const DISCOVERY_TTL_MS = 600_000;

export interface TokenSet {
  access_token: string;
  refresh_token?: string;
  expires_at: number;
}
export type RefreshResult = { ok: true; tokens: TokenSet } | { ok: false };

interface Entry {
  promise: Promise<RefreshResult>;
  settledAt?: number;
  ok?: boolean;
}
const entries = new Map<string, Entry>();
let discovery: { tokenEndpoint: string; at: number } | undefined;

/** Back channel: replace the public issuer prefix by OIDC_INTERNAL_URL. */
export function toBackChannel(url: string): string {
  const { issuer, internalUrl } = appConfig().oidc;
  if (internalUrl !== "" && url.startsWith(issuer)) return internalUrl + url.slice(issuer.length);
  return url;
}

/**
 * Auth.js's `customFetch` for the OIDC provider: every server-to-issuer call goes through the back channel. This module is the
 * server's one OIDC network client (the browser's is lib/api.ts, which only talks to the same-origin BFF).
 */
export function backChannelFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  if (typeof input === "string") return fetch(toBackChannel(input), init);
  if (input instanceof URL) return fetch(toBackChannel(input.href), init);
  return fetch(new Request(toBackChannel(input.url), input), init);
}

async function tokenEndpoint(): Promise<string> {
  if (discovery && Date.now() - discovery.at < DISCOVERY_TTL_MS) return discovery.tokenEndpoint;
  const { issuer } = appConfig().oidc;
  const res = await fetch(toBackChannel(`${issuer}/.well-known/openid-configuration`));
  if (!res.ok) throw new Error("discovery_failed");
  const doc = (await res.json()) as { token_endpoint?: string };
  if (doc.token_endpoint === undefined || doc.token_endpoint === "") throw new Error("discovery_failed");
  discovery = { tokenEndpoint: doc.token_endpoint, at: Date.now() };
  return discovery.tokenEndpoint;
}

async function doRefresh(refreshToken: string): Promise<RefreshResult> {
  try {
    const { clientId, clientSecret } = appConfig().oidc;
    const res = await fetch(toBackChannel(await tokenEndpoint()), {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        refresh_token: refreshToken,
        client_id: clientId,
        client_secret: clientSecret,
      }),
    });
    if (!res.ok) return { ok: false };
    const body = (await res.json()) as {
      access_token?: string;
      refresh_token?: string;
      expires_in?: number;
    };
    if (body.access_token === undefined || body.access_token === "" || typeof body.expires_in !== "number") return { ok: false };
    return {
      ok: true,
      tokens: {
        access_token: body.access_token,
        refresh_token: body.refresh_token ?? refreshToken,
        expires_at: Math.floor(Date.now() / 1000) + body.expires_in,
      },
    };
  } catch {
    return { ok: false };
  }
}

function prune(now: number) {
  for (const [key, entry] of entries) {
    if (entry.settledAt === undefined) continue;
    const ttl = entry.ok === true ? RESULT_TTL_MS : FAILURE_TTL_MS;
    if (now - entry.settledAt > ttl) entries.delete(key);
  }
}

export function refreshSingleFlight(refreshToken: string): Promise<RefreshResult> {
  prune(Date.now());
  const existing = entries.get(refreshToken);
  if (existing) return existing.promise;
  const entry: Entry = { promise: doRefresh(refreshToken) };
  entry.promise.then((r) => {
    entry.settledAt = Date.now();
    entry.ok = r.ok;
  });
  entries.set(refreshToken, entry);
  return entry.promise;
}

/** Cached result for a session cookie that still carries the old refresh token. */
export async function refreshedFor(refreshToken: string): Promise<RefreshResult | undefined> {
  return entries.get(refreshToken)?.promise;
}

export function needsRefresh(expiresAt: number | undefined): boolean {
  return expiresAt === undefined || expiresAt === 0 || Number.isNaN(expiresAt) || Date.now() >= (expiresAt - REFRESH_LEEWAY_SECONDS) * 1000;
}
