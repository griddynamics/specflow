// The decision table of /login (lib/login-mode.ts) and the card it renders, on the server with plain props. The browser flows (the
// automatic sign-in through the stub issuer, the fixed error, sign-out) are in e2e-production/*.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";
import type * as LoginCardModule from "@/components/v2/login/LoginCard";
import { loginMode } from "@/lib/login-mode";

stubCss();
let LoginCard: (typeof LoginCardModule)["LoginCard"];
before(async () => {
  ({ LoginCard } = await import("@/components/v2/login/LoginCard"));
});

const NONE = { session: false, signedout: undefined, error: false };

test("a session without a sign-out flag goes home; with a flag it says it is still signed in", () => {
  assert.equal(loginMode({ ...NONE, session: true }), "home");
  assert.equal(loginMode({ ...NONE, session: true, error: true }), "home");
  assert.equal(loginMode({ ...NONE, session: true, signedout: "1" }), "still-signed-in");
  assert.equal(loginMode({ ...NONE, session: true, signedout: "idp" }), "still-signed-in");
  assert.equal(loginMode({ ...NONE, session: true, signedout: "x" }), "home");
});

test("no session: sign in at once, unless there was an error or a local-only sign-out", () => {
  assert.equal(loginMode(NONE), "auto");
  assert.equal(loginMode({ ...NONE, signedout: "idp" }), "auto");
  assert.equal(loginMode({ ...NONE, signedout: "x" }), "auto");
  assert.equal(loginMode({ ...NONE, error: true }), "error");
  assert.equal(loginMode({ ...NONE, error: true, signedout: "idp" }), "error");
  assert.equal(loginMode({ ...NONE, signedout: "1" }), "signed-out");
});

const markup = (mode: "auto" | "error" | "signed-out" | "still-signed-in") =>
  renderToStaticMarkup(createElement(LoginCard, { mode, home: "/" }));

test("the card: the wordmark, one message per mode and always the Sign in button", () => {
  const texts = {
    auto: ["login-redirecting", "Taking you to sign-in"],
    error: ["login-error", "Sign-in did not work. Try again."],
    "signed-out": ["login-signed-out", "You are signed out."],
    "still-signed-in": ["login-still-signed-in", "You are still signed in"],
  } as const;
  for (const mode of Object.keys(texts) as (keyof typeof texts)[]) {
    const view = markup(mode);
    const [id, text] = texts[mode];
    assert.match(view, new RegExp(`data-testid="${id}"`), mode);
    assert.ok(view.includes(text), mode);
    assert.match(view, /data-testid="login-oidc"[^>]*>Sign in</, mode);
    assert.ok(view.includes("Graphico"), mode);
    assert.doesNotMatch(view, /<h1|Specflow|Grid Dynamics/, mode);
    for (const other of Object.values(texts)) if (other[0] !== id) assert.doesNotMatch(view, new RegExp(other[0]), mode);
  }
  assert.match(markup("error"), /role="alert"/);
});
