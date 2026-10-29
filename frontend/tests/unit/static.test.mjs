// AC-14.2 (static Dockerfile checks) and AC-12.1 (matcher), no build or server needed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { root } from "../support/servers.mjs";

const dockerfile = readFileSync(path.join(root, "Dockerfile"), "utf8");
// Join `\` continuations so multi-line ENV/HEALTHCHECK read as one instruction.
const lines = dockerfile
  .replace(/\\\n\s*/g, " ")
  .split("\n")
  .map((l) => l.trim());

function stage(name) {
  const start = lines.findIndex((l) => new RegExp(`^FROM .* AS ${name}$`, "i").test(l));
  assert.ok(start >= 0, `stage ${name} exists`);
  let end = lines.findIndex((l, i) => i > start && /^FROM /i.test(l));
  if (end < 0) end = lines.length;
  return lines.slice(start, end);
}

// No dev stage since 6a99779f: live reload runs on the host (`make frontend-dev`).
test("AC-14.2: stages deps, build, runtime in order", () => {
  const names = lines.filter((l) => /^FROM /i.test(l)).map((l) => l.split(/ as /i)[1]);
  assert.deepEqual(names, ["deps", "build", "runtime"]);
});

test("AC-14.4: exact base image tag, no floating tags", () => {
  assert.ok(!lines.some((l) => /^ARG /.test(l)), "no ARG indirection for the tag");
  assert.equal(lines.filter((l) => l.startsWith("FROM node:24.21.0-alpine AS ")).length, 2);
  for (const l of lines.filter((l) => /^FROM /i.test(l))) {
    assert.ok(/^FROM (node:24\.21\.0-alpine|deps|build) AS /i.test(l), l);
  }
});

test("AC-14.2: runtime copies standalone, .next/static and public, runs as node", () => {
  const runtime = stage("runtime");
  const copies = runtime.filter((l) => l.startsWith("COPY")).join("\n");
  assert.match(copies, /\/app\/\.next\/standalone/);
  assert.match(copies, /\/app\/\.next\/static/);
  assert.match(copies, /\/app\/public/);
  assert.ok(runtime.includes("USER node"));
  assert.ok(runtime.some((l) => /^ENV .*HOSTNAME=0\.0\.0\.0/.test(l)));
  assert.ok(runtime.some((l) => /PORT=3000/.test(l)));
  assert.ok(runtime.some((l) => /^HEALTHCHECK .*\/api\/health/.test(l)));
});

test("AC-14.2: runtime CMD runs check-env.mjs then server.js", () => {
  const cmd = stage("runtime")
    .filter((l) => l.startsWith("CMD"))
    .at(-1);
  assert.ok(cmd, "CMD present");
  const iCheck = cmd.indexOf("scripts/check-env.mjs");
  const iServer = cmd.indexOf("server.js");
  assert.ok(iCheck >= 0 && iServer > iCheck, cmd);
  assert.match(cmd, /&&/);
  assert.match(cmd, /HOSTNAME=0\.0\.0\.0 exec node server\.js/, "HOSTNAME set for the server process, exec for SIGTERM");
});

test("deps stage installs the locked dependencies; build stage runs the build", () => {
  assert.ok(stage("build").some((l) => /npm run build/.test(l)));
  assert.ok(stage("deps").some((l) => /npm ci/.test(l)));
});

test("AC-12.1: proxy.ts matcher is exactly the specified string", () => {
  const src = readFileSync(path.join(root, "proxy.ts"), "utf8");
  assert.ok(src.includes('matcher: ["/((?!api(?:/|$)|_next/|favicon\\\\.ico).*)"]'));
});

test("AC-12.1: the matcher never matches /api/backend/*, nor /api or /_next", () => {
  // Build the RegExp from the matcher string in proxy.ts itself (not a retyped copy).
  const literal = /matcher:\s*\[("(?:[^"\\]|\\.)*")\]/.exec(readFileSync(path.join(root, "proxy.ts"), "utf8"));
  assert.ok(literal, "matcher literal found in proxy.ts");
  const re = new RegExp(`^${JSON.parse(literal[1])}$`);
  for (const p of ["/api/backend/projects/x/documents", "/api/auth/session", "/api/health", "/api", "/_next/static/a.js", "/favicon.ico"]) {
    assert.ok(!re.test(p), p);
  }
  for (const p of ["/", "/login", "/projects/1", "/projects/1/documents/2", "/apiary"]) assert.ok(re.test(p), p);
});

test("AC-12.1: no route folder under app/ starts with an underscore", () => {
  const bad = [];
  (function walk(dir) {
    for (const name of readdirSync(dir)) {
      const full = path.join(dir, name);
      if (!statSync(full).isDirectory()) continue;
      if (name.startsWith("_")) bad.push(full);
      walk(full);
    }
  })(path.join(root, "app"));
  assert.deepEqual(bad, []);
});

test("AC-6.6: the BFF proxies with node:http and never with fetch", () => {
  const src = readFileSync(path.join(root, "app/api/backend/[...path]/route.ts"), "utf8");
  assert.match(src, /from "node:http"/);
  assert.match(src, /Readable\.fromWeb/);
  assert.doesNotMatch(src, /\bfetch\(/);
});

test("AC-12.5: no NEXT_PUBLIC_ variable is used for configuration", () => {
  const hits = [];
  (function walk(dir) {
    for (const name of readdirSync(dir)) {
      if (["node_modules", ".next", "tests", "resources"].includes(name)) continue;
      const full = path.join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (/\.(ts|tsx|mjs|js|json)$/.test(name) && name !== "package-lock.json" && /NEXT_PUBLIC_/.test(readFileSync(full, "utf8")))
        hits.push(full);
    }
  })(root);
  assert.deepEqual(hits, []);
});

test("next.config.ts sets output standalone", () => {
  assert.match(readFileSync(path.join(root, "next.config.ts"), "utf8"), /output:\s*"standalone"/);
});

test("M5: the auth route pins every request to PUBLIC_URL before Auth.js sees it", () => {
  const src = readFileSync(path.join(root, "app/api/auth/[...nextauth]/route.ts"), "utf8");
  assert.match(src, /headers\.set\("x-forwarded-host", target\.host\)/);
  assert.match(src, /headers\.set\("x-forwarded-proto"/);
  assert.match(src, /new Request\(publicOrigin \+ url\.pathname/);
  assert.match(src, /handlers\.GET\(await pinned\(req\)\)/);
  assert.match(src, /handlers\.POST\(await pinned\(req\)\)/);
});
