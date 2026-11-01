// AC-6.4 / AC-2.6 / AC-14.3 (frontend side): check-env.mjs fails closed, names variables only.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomSecret, root } from "../support/servers.mjs";
import { mcpClientIdOf, mcpPublicUrlOf, normalizeInternalUrl, validateEnv } from "../../lib/env-rules.mjs";

const script = path.join(root, "scripts/check-env.mjs");
const SECRET = randomSecret();
const KEY = randomSecret();

const localDev = {
  APP_ENV: "local",
  AUTH_MODE: "dev",
  PUBLIC_URL: "http://127.0.0.1:3201",
  API_INTERNAL_URL: "http://127.0.0.1:8000",
  SESSION_SECRET: SECRET,
  AUTH_DEV_SIGNING_KEY: KEY,
};
const nonLocalOidc = {
  APP_ENV: "dev",
  AUTH_MODE: "oidc",
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

test("valid local dev env passes", () => assert.equal(run(localDev).code, 0));

test("control run: valid non-local oidc env without a dev key exits 0 (AC-14.3)", () => {
  assert.equal(run(nonLocalOidc).code, 0);
});

test("AUTH_MODE=dev outside local exits non-zero naming AUTH_MODE (AC-14.3, AC-6.4)", () => {
  const r = run({ ...nonLocalOidc, AUTH_MODE: "dev" });
  assert.equal(r.code, 2);
  assert.match(r.out, /AUTH_MODE/);
});

test("APP_ENV must be exactly local for dev mode (case, whitespace)", () => {
  for (const appEnv of ["Local", "local ", "LOCAL", "dev", "prod"]) {
    assert.ok(validateEnv({ ...localDev, APP_ENV: appEnv }).length > 0, appEnv);
  }
});

test("KUBERNETES_SERVICE_HOST refuses dev mode, even when empty", () => {
  for (const v of ["10.0.0.1", ""]) {
    const r = run({ ...localDev, KUBERNETES_SERVICE_HOST: v });
    assert.equal(r.code, 2);
    assert.match(r.out, /AUTH_MODE/);
  }
});

test("a dev signing key is refused unless dev mode is allowed", () => {
  const r = run({ ...nonLocalOidc, AUTH_DEV_SIGNING_KEY: KEY });
  assert.equal(r.code, 2);
  assert.match(r.out, /AUTH_DEV_SIGNING_KEY/);
});

test("oidc mode with a dev signing key is refused in every environment, including local", () => {
  for (const appEnv of ["local", "dev", "prod"]) {
    const r = run({ ...nonLocalOidc, APP_ENV: appEnv, AUTH_DEV_SIGNING_KEY: KEY });
    assert.equal(r.code, 2, appEnv);
    assert.match(r.out, /AUTH_DEV_SIGNING_KEY/);
    assert.ok(!r.out.includes(KEY));
  }
});

test("SESSION_SECRET under 32 bytes is refused; dev key too", () => {
  assert.match(run({ ...localDev, SESSION_SECRET: "short" }).out, /SESSION_SECRET/);
  assert.match(run({ ...localDev, AUTH_DEV_SIGNING_KEY: "short" }).out, /AUTH_DEV_SIGNING_KEY/);
});

test("oidc mode requires issuer, client id and secret", () => {
  const r = run({ ...nonLocalOidc, OIDC_ISSUER: "", OIDC_CLIENT_SECRET: "" });
  assert.equal(r.code, 2);
  assert.match(r.out, /OIDC_ISSUER/);
  assert.match(r.out, /OIDC_CLIENT_SECRET/);
});

test("an empty env names every required variable, and no value is ever printed", () => {
  const r = run({});
  assert.equal(r.code, 2);
  for (const name of ["APP_ENV", "PUBLIC_URL", "API_INTERNAL_URL", "SESSION_SECRET"]) {
    assert.match(r.out, new RegExp(name));
  }
  const leaky = run({ ...localDev, AUTH_MODE: "bogus" });
  for (const secret of [SECRET, KEY]) assert.ok(!leaky.out.includes(secret));
});

test("a missing or empty AUTH_MODE means oidc: the oidc variables are required and a dev key is refused (SPL-PKG-02)", () => {
  const noMode = { ...nonLocalOidc };
  delete noMode.AUTH_MODE;
  assert.deepEqual(validateEnv(noMode), []);
  assert.deepEqual(validateEnv({ ...noMode, AUTH_MODE: "" }), []);
  const noIssuer = { ...noMode };
  delete noIssuer.OIDC_ISSUER;
  assert.deepEqual(validateEnv(noIssuer), ["OIDC_ISSUER"]);
  assert.deepEqual(validateEnv({ ...noMode, APP_ENV: "local", AUTH_DEV_SIGNING_KEY: KEY }), ["AUTH_DEV_SIGNING_KEY"]);
});

test("--load-dotenv reads .env.local like Next does (host dev/start); without it the file is ignored", () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), "specflow-fe-env-"));
  try {
    writeFileSync(
      path.join(dir, ".env.local"),
      Object.entries(localDev)
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
  const empty = { ...env, MCP_PUBLIC_URL: "", MCP_CLIENT_ID: "" };
  assert.equal(mcpPublicUrlOf(empty), "https://specflow.example.test/mcp");
  assert.equal(mcpClientIdOf(empty), "specflow-mcp");
  assert.deepEqual(validateEnv(empty), []);
});

test("MCP settings: set values are used and valid", () => {
  const env = { ...nonLocalOidc, MCP_PUBLIC_URL: "https://mcp.example.test/mcp", MCP_CLIENT_ID: "my-client" };
  assert.equal(mcpPublicUrlOf(env), "https://mcp.example.test/mcp");
  assert.equal(mcpClientIdOf(env), "my-client");
  assert.deepEqual(validateEnv(env), []);
  assert.deepEqual(validateEnv({ ...localDev, MCP_PUBLIC_URL: "http://localhost:8002/mcp" }), []);
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
