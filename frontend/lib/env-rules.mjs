// Frontend environment rules. One source for scripts/check-env.mjs (startup
// gate), instrumentation.ts and auth.ts. Returns variable NAMES only, never
// values (AC-6.4, N5). Plain .mjs so the runtime image can run it without a build.

const APP_ENVS = ["local", "dev", "prod"];
export const MIN_SECRET_BYTES = 32;
/** Shown with the failure line when a removed AUTH_MODE=dev is still set. */
export const AUTH_MODE_DEV_HINT =
  "AUTH_MODE=dev was removed: delete AUTH_MODE and AUTH_DEV_SIGNING_KEY from your env file; local sign-in uses the bundled Keycloak (QUICKSTART.md)";
/** MCP_CLIENT_ID default: the first client id the mcp role accepts. */
export const DEFAULT_MCP_CLIENT_ID = "specflow-mcp";
/** MCP_SERVER_NAME default: the name `claude mcp add` registers the server under. */
export const DEFAULT_MCP_SERVER_NAME = "specflow";
const MCP_SERVER_NAME_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

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

/**
 * The MCP server name shown in the Guides: MCP_SERVER_NAME, or "specflow" when it is unset or empty.
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function mcpServerNameOf(env) {
  return isSet(env.MCP_SERVER_NAME) ? env.MCP_SERVER_NAME.trim() : DEFAULT_MCP_SERVER_NAME;
}

/** @returns {string[]} sorted, unique names of invalid variables */
export function validateEnv(env) {
  const bad = new Set();
  const need = (name, ok = isSet) => {
    if (!ok(env[name])) bad.add(name);
  };

  need("APP_ENV", (v) => APP_ENVS.includes(v));
  // The dev sign-in is gone: a stale AUTH_MODE=dev is refused, any other value (or none) is ignored.
  if (env.AUTH_MODE?.trim().toLowerCase() === "dev") bad.add("AUTH_MODE");
  need("PUBLIC_URL", isHttpUrl);
  need("API_INTERNAL_URL", (v) => isHttpUrl(normalizeInternalUrl(v)));
  need("SESSION_SECRET", (v) => isSet(v) && Buffer.byteLength(v) >= MIN_SECRET_BYTES);

  // Optional MCP settings for the Guides page. Empty means unset (as in .env.example); whitespace only is a mistake.
  if (isSet(env.MCP_PUBLIC_URL)) {
    need("MCP_PUBLIC_URL", (v) => isHttpUrl(v.trim()) && (env.APP_ENV === "local" || v.trim().startsWith("https://")));
  }
  if (typeof env.MCP_CLIENT_ID === "string" && env.MCP_CLIENT_ID !== "" && !isSet(env.MCP_CLIENT_ID)) bad.add("MCP_CLIENT_ID");

  // Empty means unset (default); anything else must be a plain name (it becomes a tool prefix `mcp__<name>__`).
  if (typeof env.MCP_SERVER_NAME === "string" && env.MCP_SERVER_NAME !== "" && !MCP_SERVER_NAME_PATTERN.test(env.MCP_SERVER_NAME.trim())) {
    bad.add("MCP_SERVER_NAME");
  }

  need("OIDC_ISSUER", isHttpUrl);
  need("OIDC_CLIENT_ID");
  need("OIDC_CLIENT_SECRET");
  if (isSet(env.OIDC_INTERNAL_URL)) need("OIDC_INTERNAL_URL", isHttpUrl);
  return [...bad].sort();
}

/** Human-readable single line, names only (plus the fixed hint for a removed AUTH_MODE=dev). */
export function failureLine(variables) {
  return JSON.stringify({
    event: "config.validation.failed",
    role: "frontend",
    variables,
    ...(variables.includes("AUTH_MODE") ? { hint: AUTH_MODE_DEV_HINT } : {}),
  });
}
