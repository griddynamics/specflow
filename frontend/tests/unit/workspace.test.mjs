// The npm workspace: one root install and lock, packages/ui resolvable by the app, Next roots at the workspace root.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import { root } from "../support/servers.mjs";

const repo = path.join(root, "..");
const readJson = (...parts) => JSON.parse(readFileSync(path.join(repo, ...parts), "utf8"));

test("the root package is private and lists packages/ui and frontend as workspaces", () => {
  const pkg = readJson("package.json");
  assert.equal(pkg.private, true);
  assert.ok(pkg.workspaces.includes("packages/ui") && pkg.workspaces.includes("frontend"), String(pkg.workspaces));
});

test("there is one lockfile, at the root, and it lists every workspace", () => {
  assert.ok(!existsSync(path.join(root, "package-lock.json")), "frontend/package-lock.json is gone");
  const lock = readJson("package-lock.json");
  for (const dir of readJson("package.json").workspaces) assert.ok(lock.packages[dir], `${dir} is in the lock`);
});

test("the frontend depends on @graphico/ui, which resolves to packages/ui", () => {
  assert.ok(readJson("frontend", "package.json").dependencies["@graphico/ui"]);
  assert.equal(readJson("packages", "ui", "package.json").name, "@graphico/ui");
  assert.equal(realpathSync(path.join(repo, "node_modules/@graphico/ui")), realpathSync(path.join(repo, "packages/ui")));
});

test("next.config.ts transpiles the package and pins both roots to the workspace root", () => {
  const src = readFileSync(path.join(root, "next.config.ts"), "utf8");
  assert.match(src, /transpilePackages:\s*\["@graphico\/ui"\]/);
  assert.match(src, /outputFileTracingRoot:\s*workspaceRoot/);
  assert.match(src, /turbopack:\s*\{\s*root:\s*workspaceRoot\s*\}/);
  assert.match(src, /workspaceRoot = path\.join\(__dirname, "\.\."\)/);
});
