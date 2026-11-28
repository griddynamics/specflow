// MCP-M10: what stays of the Guides in V2 — the MCP dialog's commands. `guideConfig()` (lib/guides/guide-config.ts) reads the
// deployment's MCP values and the Integrations page passes them to `McpSetup` (components/v2/integrations/McpSetup.tsx), whose three
// copy rows are built by `lib/guides/snippets`: the add command from the values, the constant sign-in, and the bind command with the
// configured server name and the open project. The Guides routes, topics, sidebar section, hub cards and agent picker were the V1 pages'
// subjects and die with them. The builders' exhaustive coverage is guides-snippets.test.ts; the env defaults' is mcp-config.test.ts.
import assert from "node:assert/strict";
import { test } from "node:test";
import { guideConfig } from "@/lib/guides/guide-config";
import { bindCommand, claudeAddCommand, SIGN_IN_COMMAND } from "@/lib/guides/snippets";

const PID = "0b8b2c1e-6f3a-4c52-9d1e-7a5f4b3c2d10";
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

test("guideConfig reads the deployment's MCP values; McpSetup's three commands follow", () => {
  const saved = { ...process.env };
  try {
    Object.assign(process.env, BASE, {
      MCP_PUBLIC_URL: "https://mcp.example.test/mcp",
      MCP_CLIENT_ID: "acme-client",
      MCP_SERVER_NAME: "acme-specflow",
    });
    const mcp = guideConfig();
    assert.deepEqual(mcp, { mcpUrl: "https://mcp.example.test/mcp", clientId: "acme-client", serverName: "acme-specflow" });
    assert.equal(
      claudeAddCommand(mcp),
      "claude mcp add --transport http --scope user --client-id acme-client --callback-port 8765 acme-specflow https://mcp.example.test/mcp",
    );
    assert.equal(SIGN_IN_COMMAND, "/mcp");
    assert.equal(bindCommand(mcp.serverName, PID), `/mcp__acme-specflow__select_project ${PID}`);
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) Reflect.deleteProperty(process.env, k);
    Object.assign(process.env, saved);
  }
});
