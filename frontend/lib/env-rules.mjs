// Frontend environment rules. One source for scripts/check-env.mjs (startup
// gate), instrumentation.ts and auth.ts. Returns variable NAMES only, never
// values (AC-6.4, N5). Plain .mjs so the runtime image can run it without a build.

const APP_ENVS = ["local", "dev", "prod"];
const AUTH_MODES = ["oidc", "dev"];
export const MIN_SECRET_BYTES = 32;
/** AUTH_MODE default (SPL-PKG-02): a missing or empty AUTH_MODE means oidc, so the oidc rules apply. */
export const DEFAULT_AUTH_MODE = "oidc";
/** MCP_CLIENT_ID default: the first client id the mcp role accepts. */
export const DEFAULT_MCP_CLIENT_ID = "specflow-mcp";

const isSet = (v) => typeof v === "string" && v.trim() !== "";
const isHttpUrl = (v) => {
  try {
    return ["http:", "https:"].includes(new URL(v).protocol);
  } catch {
    return false;
  }
};

/**
 * API_INTERNAL_URL is an in-cluster address, often a bare Service name
 * ("specflow-api", "specflow-api:8000"). Add http:// when no scheme is given;
 * drop trailing slashes. Used by the validator and by lib/config.ts.
 */
export function normalizeInternalUrl(v) {
  const s = typeof v === "string" ? v.trim() : "";
  if (s === "") return "";
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(s) ? s : `http://${s}`;
  return withScheme.replace(/([^/:])\/+$/, "$1");
}

/** AC-6.4: dev mode only for APP_ENV exactly "local" and no Kubernetes. Fails closed. */
export function devModeAllowed(env) {
  return env.APP_ENV === "local" && env.KUBERNETES_SERVICE_HOST === undefined;
}

/** The effective auth mode: AUTH_MODE, or "oidc" when it is missing or empty. */
export function authModeOf(env) {
  return isSet(env.AUTH_MODE) ? env.AUTH_MODE : DEFAULT_AUTH_MODE;
}

/**
 * The MCP address shown in the Guides: MCP_PUBLIC_URL, or PUBLIC_URL + "/mcp" when it is unset or empty.
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function mcpPublicUrlOf(env) {
  if (isSet(env.MCP_PUBLIC_URL)) return env.MCP_PUBLIC_URL.trim();
  return `${(env.PUBLIC_URL ?? "").trim().replace(/\/+$/, "")}/mcp`;
}

/**
 * The OAuth client id shown in the Guides: MCP_CLIENT_ID, or "specflow-mcp" when it is unset or empty.
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function mcpClientIdOf(env) {
  return isSet(env.MCP_CLIENT_ID) ? env.MCP_CLIENT_ID.trim() : DEFAULT_MCP_CLIENT_ID;
}

/** @returns {string[]} sorted, unique names of invalid variables */
export function validateEnv(env) {
  const bad = new Set();
  const authMode = authModeOf(env);
  const need = (name, ok = isSet) => {
    if (!ok(env[name])) bad.add(name);
  };

  need("APP_ENV", (v) => APP_ENVS.includes(v));
  if (isSet(env.AUTH_MODE)) need("AUTH_MODE", (v) => AUTH_MODES.includes(v));
  need("PUBLIC_URL", isHttpUrl);
  need("API_INTERNAL_URL", (v) => isHttpUrl(normalizeInternalUrl(v)));
  need("SESSION_SECRET", (v) => isSet(v) && Buffer.byteLength(v) >= MIN_SECRET_BYTES);

  // Optional MCP settings for the Guides page. Empty means unset (as in .env.example); whitespace only is a mistake.
  if (isSet(env.MCP_PUBLIC_URL)) {
    need("MCP_PUBLIC_URL", (v) => isHttpUrl(v.trim()) && (env.APP_ENV === "local" || v.trim().startsWith("https://")));
  }
  if (typeof env.MCP_CLIENT_ID === "string" && env.MCP_CLIENT_ID !== "" && !isSet(env.MCP_CLIENT_ID)) bad.add("MCP_CLIENT_ID");

  const devAllowed = devModeAllowed(env);
  if (authMode === "dev") {
    if (!devAllowed) bad.add("AUTH_MODE");
    need("AUTH_DEV_SIGNING_KEY", (v) => isSet(v) && Buffer.byteLength(v) >= MIN_SECRET_BYTES);
  }
  if (isSet(env.AUTH_DEV_SIGNING_KEY) && !devAllowed) bad.add("AUTH_DEV_SIGNING_KEY");
  // As the backend (AC-1.4): oidc with a dev key is refused in EVERY environment.
  if (authMode === "oidc" && isSet(env.AUTH_DEV_SIGNING_KEY)) bad.add("AUTH_DEV_SIGNING_KEY");

  if (authMode === "oidc") {
    need("OIDC_ISSUER", isHttpUrl);
    need("OIDC_CLIENT_ID");
    need("OIDC_CLIENT_SECRET");
    if (isSet(env.OIDC_INTERNAL_URL)) need("OIDC_INTERNAL_URL", isHttpUrl);
  }
  return [...bad].sort();
}

/** Human-readable single line, names only. */
export function failureLine(variables) {
  return JSON.stringify({
    event: "config.validation.failed",
    role: "frontend",
    variables,
  });
}
