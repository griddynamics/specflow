// AC-6.4 / AC-2.6 / AC-14.3 (frontend side): check-env.mjs fails closed, names variables only.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomSecret, root } from "../support/servers.mjs";
import {
  AUTH_MODE_DEV_HINT,
  mcpClientIdOf,
  mcpPublicUrlOf,
  mcpServerNameOf,
  normalizeInternalUrl,
  validateEnv,
} from "../../lib/env-rules.mjs";

const script = path.join(root, "scripts/check-env.mjs");
const SECRET = randomSecret();
const KEY = randomSecret();

const local = {
  APP_ENV: "local",
  PUBLIC_URL: "http://127.0.0.1:3201",
  API_INTERNAL_URL: "http://127.0.0.1:8000",
  SESSION_SECRET: SECRET,
  OIDC_ISSUER: "http://127.0.0.1:8180/realms/specflow",
  OIDC_CLIENT_ID: "specflow-web",
  OIDC_CLIENT_SECRET: randomSecret(),
};
const nonLocalOidc = {
  APP_ENV: "dev",
  PUBLIC_URL: "https://specflow.example.test",
  API_INTERNAL_URL: "http://api:8000",
  SESSION_SECRET: SECRET,
  OIDC_ISSUER: "https://kc.example.test/realms/gd",
  OIDC_CLIENT_ID: "specflow-web",
  OIDC_CLIENT_SECRET: randomSecret(),
};

function run(env) {
  const r = spawnSync(process.execPath, [script], { env: { PATH: process.env.PATH, ...env }, encoding: "utf8" });
  return { code: r.status, out: r.stdout + r.stderr };
}

test("a valid local env passes, with the same OIDC rules as any other (no dev mode)", () => {
  assert.equal(run(local).code, 0);
  assert.deepEqual(validateEnv({ ...local, OIDC_ISSUER: "" }), ["OIDC_ISSUER"]);
});

test("control run: a valid non-local oidc env exits 0 (AC-14.3)", () => {
  assert.equal(run(nonLocalOidc).code, 0);
});

test("AUTH_MODE=dev is refused in every environment, with a one-line hint and no value (B-06)", () => {
  for (const appEnv of ["local", "dev", "prod"]) {
    const r = run({ ...nonLocalOidc, APP_ENV: appEnv, AUTH_MODE: "dev" });
    assert.equal(r.code, 2, appEnv);
    assert.equal(r.out.trim().split("\n").length, 1, "one line");
    const line = JSON.parse(r.out);
    assert.equal(line.event, "config.validation.failed");
    assert.deepEqual(line.variables, ["AUTH_MODE"]);
    assert.equal(line.hint, AUTH_MODE_DEV_HINT);
    assert.match(line.hint, /delete AUTH_MODE and AUTH_DEV_SIGNING_KEY/);
  }
});

test("AUTH_MODE=dev is refused in any case and with surrounding spaces, as in the backend", () => {
  for (const mode of ["DEV", " dev ", "Dev"]) assert.deepEqual(validateEnv({ ...nonLocalOidc, AUTH_MODE: mode }), ["AUTH_MODE"], mode);
});

test("AUTH_MODE=oidc, any other value and an empty one are ignored", () => {
  for (const mode of ["oidc", "", "bogus", undefined])
    assert.deepEqual(validateEnv({ ...nonLocalOidc, AUTH_MODE: mode }), [], String(mode));
});

test("a stale AUTH_DEV_SIGNING_KEY is ignored in every environment and never printed", () => {
  for (const appEnv of ["local", "dev", "prod"]) {
    const env = { ...nonLocalOidc, APP_ENV: appEnv, AUTH_DEV_SIGNING_KEY: KEY, LOCAL_TEST_USER_EMAIL: "dev@example.test" };
    assert.deepEqual(validateEnv(env), [], appEnv);
    const r = run(env);
    assert.equal(r.code, 0, appEnv);
    assert.ok(!r.out.includes(KEY));
  }
  const r = run({ ...nonLocalOidc, AUTH_MODE: "dev", AUTH_DEV_SIGNING_KEY: KEY });
  assert.deepEqual(JSON.parse(r.out).variables, ["AUTH_MODE"], "only AUTH_MODE is named");
  assert.ok(!r.out.includes(KEY));
});

test("SESSION_SECRET under 32 bytes is refused", () => {
  assert.match(run({ ...local, SESSION_SECRET: "short" }).out, /SESSION_SECRET/);
});

test("the OIDC variables are always required: issuer, client id and secret", () => {
  const r = run({ ...nonLocalOidc, OIDC_ISSUER: "", OIDC_CLIENT_SECRET: "" });
  assert.equal(r.code, 2);
  assert.match(r.out, /OIDC_ISSUER/);
  assert.match(r.out, /OIDC_CLIENT_SECRET/);
  assert.deepEqual(validateEnv({ ...local, OIDC_INTERNAL_URL: "not a url" }), ["OIDC_INTERNAL_URL"]);
});

test("an empty env names every required variable, and no value is ever printed", () => {
  const r = run({});
  assert.equal(r.code, 2);
  for (const name of ["APP_ENV", "PUBLIC_URL", "API_INTERNAL_URL", "SESSION_SECRET"]) {
    assert.match(r.out, new RegExp(name));
  }
  const leaky = run({ ...local, SESSION_SECRET: "short", AUTH_DEV_SIGNING_KEY: KEY });
  for (const secret of [SECRET, KEY, local.OIDC_CLIENT_SECRET]) assert.ok(!leaky.out.includes(secret));
});

test("--load-dotenv reads .env.local like Next does (host dev/start); without it the file is ignored", () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), "specflow-fe-env-"));
  try {
    writeFileSync(
      path.join(dir, ".env.local"),
      Object.entries(local)
        .map(([k, v]) => `${k}=${v}`)
        .join("\n"),
    );
    const run2 = (args) => spawnSync(process.execPath, [script, ...args], { cwd: dir, env: { PATH: process.env.PATH }, encoding: "utf8" });
    assert.equal(run2(["--load-dotenv", "--dev"]).status, 0);
    assert.equal(run2([]).status, 2);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("API_INTERNAL_URL accepts a bare Service name and adds http://", () => {
  for (const v of ["specflow--api--prod", "specflow-api:8000", "http://api:8000", "https://api.internal/"]) {
    assert.deepEqual(validateEnv({ ...nonLocalOidc, API_INTERNAL_URL: v }), [], v);
  }
  assert.equal(normalizeInternalUrl("specflow--api--prod"), "http://specflow--api--prod");
  assert.equal(normalizeInternalUrl(" specflow-api:8000/ "), "http://specflow-api:8000");
  assert.equal(normalizeInternalUrl("https://api.internal/"), "https://api.internal");
});

test("API_INTERNAL_URL is still required and must parse", () => {
  for (const v of [undefined, "", "   ", "ftp://api", "http://"]) {
    assert.deepEqual(validateEnv({ ...nonLocalOidc, API_INTERNAL_URL: v }), ["API_INTERNAL_URL"], String(v));
  }
});

// MCP settings of the Guides page: optional, with defaults, validated when set.
test("MCP settings: defaults derive from PUBLIC_URL and specflow-mcp", () => {
  const env = { ...nonLocalOidc, PUBLIC_URL: "https://specflow.example.test/" };
  assert.equal(mcpPublicUrlOf(env), "https://specflow.example.test/mcp");
  assert.equal(mcpClientIdOf(env), "specflow-mcp");
  assert.equal(mcpServerNameOf(env), "specflow");
  const empty = { ...env, MCP_PUBLIC_URL: "", MCP_CLIENT_ID: "", MCP_SERVER_NAME: "" };
  assert.equal(mcpPublicUrlOf(empty), "https://specflow.example.test/mcp");
  assert.equal(mcpClientIdOf(empty), "specflow-mcp");
  assert.equal(mcpServerNameOf(empty), "specflow");
  assert.deepEqual(validateEnv(empty), []);
});

test("MCP settings: set values are used and valid", () => {
  const env = {
    ...nonLocalOidc,
    MCP_PUBLIC_URL: "https://mcp.example.test/mcp",
    MCP_CLIENT_ID: "my-client",
    MCP_SERVER_NAME: "specflow-local",
  };
  assert.equal(mcpPublicUrlOf(env), "https://mcp.example.test/mcp");
  assert.equal(mcpClientIdOf(env), "my-client");
  assert.equal(mcpServerNameOf(env), "specflow-local");
  assert.deepEqual(validateEnv(env), []);
  assert.deepEqual(validateEnv({ ...local, MCP_PUBLIC_URL: "http://localhost:8002/mcp" }), []);
});

test("MCP_PUBLIC_URL must be an absolute http(s) URL, https outside local, and the gate names it", () => {
  for (const bad of ["not a url", "/mcp", "ftp://mcp.example.test/mcp", "mcp.example.test/mcp"]) {
    assert.deepEqual(validateEnv({ ...nonLocalOidc, MCP_PUBLIC_URL: bad }), ["MCP_PUBLIC_URL"], bad);
  }
  assert.deepEqual(validateEnv({ ...nonLocalOidc, MCP_PUBLIC_URL: "http://mcp.example.test/mcp" }), ["MCP_PUBLIC_URL"]);
  const r = run({ ...nonLocalOidc, MCP_PUBLIC_URL: "http://mcp.example.test/mcp" });
  assert.equal(r.code, 2);
  assert.match(r.out, /MCP_PUBLIC_URL/);
  assert.doesNotMatch(r.out, /mcp\.example\.test/);
});

test("MCP_CLIENT_ID must not be blank", () => {
  for (const bad of [" ", "   ", "\t"]) {
    assert.deepEqual(validateEnv({ ...nonLocalOidc, MCP_CLIENT_ID: bad }), ["MCP_CLIENT_ID"]);
  }
  assert.equal(run({ ...nonLocalOidc, MCP_CLIENT_ID: "  " }).code, 2);
});

test("MCP_SERVER_NAME: valid names pass, blank means the default, others are named by the gate", () => {
  for (const ok of ["specflow", "specflow-local", "My_Server-2", "a", "x".repeat(64)]) {
    assert.deepEqual(validateEnv({ ...nonLocalOidc, MCP_SERVER_NAME: ok }), [], ok);
  }
  assert.deepEqual(validateEnv({ ...nonLocalOidc, MCP_SERVER_NAME: "" }), []);
  for (const bad of ["bad name!", "  ", "a b", "x".repeat(65), "naïve", "semi;colon"]) {
    assert.deepEqual(validateEnv({ ...nonLocalOidc, MCP_SERVER_NAME: bad }), ["MCP_SERVER_NAME"], bad);
  }
  const r = run({ ...nonLocalOidc, MCP_SERVER_NAME: "bad name!" });
  assert.equal(r.code, 2);
  assert.match(r.out, /MCP_SERVER_NAME/);
  assert.doesNotMatch(r.out, /bad name!/);
});
