// A3, FE-BP-06 (TECH-SPECS §8): which paths skip login. Pure; no build needed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { skipsLogin } from "../../lib/proxy-rules.mjs";

test("A3 / FE-BP-06: /ds/**, /icon.svg, /favicon.ico and /apple-icon.png skip login", () => {
  for (const p of ["/ds", "/ds/Logos/gd-mark.png", "/icon.svg", "/favicon.ico", "/apple-icon.png"]) assert.equal(skipsLogin(p), true, p);
});

test("A3 / FE-BP-06: look-alike and other paths still need a session", () => {
  for (const p of [
    "/dsx",
    "/icon.svgx",
    "/favicon.icox",
    "/apple-icon.pngx",
    "/apple-touch-icon.png",
    "/",
    "/projects/1",
    "/api/backend/me",
  ]) {
    assert.equal(skipsLogin(p), false, p);
  }
});
