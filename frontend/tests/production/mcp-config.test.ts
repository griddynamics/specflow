// appConfig().mcp: the Guides page's address and client id, with defaults (FR-MCP-UI-03).
import assert from "node:assert/strict";
import { test } from "node:test";
import { appConfig } from "../../lib/config";

const BASE = {
  APP_ENV: "dev",
  AUTH_MODE: "oidc",
  PUBLIC_URL: "https://specflow.example.test",
  API_INTERNAL_URL: "https://api.example.test",
  SESSION_SECRET: "s".repeat(40),
  OIDC_ISSUER: "https://kc.example.test/realms/gd",
  OIDC_CLIENT_ID: "specflow-web",
  OIDC_CLIENT_SECRET: "secret-value",
};

test("appConfig().mcp defaults to PUBLIC_URL + /mcp and specflow-mcp", () => {
  const saved = { ...process.env };
  try {
    Object.assign(process.env, BASE);
    Reflect.deleteProperty(process.env, "MCP_PUBLIC_URL");
    Reflect.deleteProperty(process.env, "MCP_CLIENT_ID");
    assert.deepEqual(appConfig().mcp, { publicUrl: "https://specflow.example.test/mcp", clientId: "specflow-mcp" });
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) Reflect.deleteProperty(process.env, k);
    Object.assign(process.env, saved);
  }
});
