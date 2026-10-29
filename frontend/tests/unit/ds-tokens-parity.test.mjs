// FE-DS-04.AC1: styles/ds/tokens.css is the design-system tokens.css, plus one "Copied from" head comment line. Pure file read.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

test("FE-DS-04.AC1: frontend/styles/ds/tokens.css equals docs/design-system/tokens.css apart from its head comment", () => {
  const copy = readFileSync(path.join(root, "styles/ds/tokens.css"), "utf8");
  const source = readFileSync(path.join(root, "../docs/design-system/tokens.css"), "utf8");
  const body = copy.replace(/^\/\* Copied from [^\n]*\*\/\n/, "");
  assert.equal(body, source);
});
