import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const dockerfile = readFileSync(path.join(root, "Dockerfile"), "utf8");
const [beforeCi] = dockerfile.split(/^RUN npm ci/m);

test("the deps stage copies the root manifest, the lock and every workspace manifest before npm ci", () => {
  assert.match(beforeCi, /package\.json package-lock\.json/);
  const { workspaces } = JSON.parse(readFileSync(path.join(root, "..", "package.json"), "utf8")) as { workspaces: string[] };
  for (const dir of workspaces) assert.ok(beforeCi.includes(`${dir}/package.json`), `${dir}/package.json is copied before npm ci`);
});

test("every base image has an exact tag and the runtime stage is an unprivileged static server on 8080", () => {
  for (const [, image] of dockerfile.matchAll(/^FROM\s+(\S+)/gm)) {
    if (image === "deps" || image === "build") continue;
    assert.match(image, /:\d+\.\d+\.\d+/, `${image} is pinned`);
  }
  const runtime = dockerfile.split("AS runtime")[1];
  assert.match(runtime, /^USER \d+:\d+$/m);
  assert.match(runtime, /^EXPOSE 8080$/m);
  assert.doesNotMatch(runtime, /\bnode\b/, "the runtime image carries no Node");
});

test("the static server maps the 404 page that the export writes", () => {
  assert.equal(readFileSync(path.join(root, "httpd.conf"), "utf8").trim(), "E404:404.html");
});
