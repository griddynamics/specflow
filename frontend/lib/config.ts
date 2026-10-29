// Server-only: the one module that reads the server's environment (the proxy, Auth.js, the BFF and the server layouts read it
// through here). It never reaches the browser bundle.
import { authModeOf, devModeAllowed, normalizeInternalUrl, validateEnv } from "./env-rules.mjs";

/** The server process environment, read at call time (never inlined at build: these are runtime variables). */
export function serverEnv(): NodeJS.ProcessEnv {
  return process.env;
}

export interface AppConfig {
  authMode: "oidc" | "dev";
  publicUrl: string;
  publicOrigin: string;
  secure: boolean;
  apiInternalUrl: string;
  sessionSecret: string;
  devSigningKey: string;
  localTestUserEmail: string;
  oidc: { issuer: string; internalUrl: string; clientId: string; clientSecret: string };
}

/** AC-6.7: __Host- prefix needs Secure, so it is only used on https. */
export function sessionCookieName(publicUrl: string | undefined): string {
  return publicUrl?.startsWith("https://") === true ? "__Host-specflow-session" : "specflow-session";
}

function stripTrailingSlashes(value: string): string {
  let end = value.length;
  while (end > 0 && value[end - 1] === "/") end -= 1;
  return value.slice(0, end);
}

/** Throws (fail closed, AC-6.4) naming variables only. Memoised once valid. */
let cached: AppConfig | undefined;
export function appConfig(): AppConfig {
  if (cached) return cached;
  const env = serverEnv();
  const bad = validateEnv(env);
  if (bad.length > 0) {
    throw new Error(`config.validation.failed variables=${bad.join(",")}`);
  }
  const authMode = authModeOf(env) as "oidc" | "dev";
  if (authMode === "dev" && !devModeAllowed(env)) {
    throw new Error("config.validation.failed variables=AUTH_MODE");
  }
  const publicUrl = env.PUBLIC_URL ?? ""; // validateEnv above guarantees it is set
  cached = {
    authMode,
    publicUrl,
    publicOrigin: new URL(publicUrl).origin,
    secure: publicUrl.startsWith("https://"),
    apiInternalUrl: normalizeInternalUrl(env.API_INTERNAL_URL),
    sessionSecret: env.SESSION_SECRET ?? "",
    devSigningKey: env.AUTH_DEV_SIGNING_KEY ?? "",
    localTestUserEmail: env.LOCAL_TEST_USER_EMAIL ?? "",
    oidc: {
      issuer: stripTrailingSlashes(env.OIDC_ISSUER ?? ""),
      internalUrl: stripTrailingSlashes(env.OIDC_INTERNAL_URL ?? ""),
      clientId: env.OIDC_CLIENT_ID ?? "",
      clientSecret: env.OIDC_CLIENT_SECRET ?? "",
    },
  };
  return cached;
}
