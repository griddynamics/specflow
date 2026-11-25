// SPECS §12 item 2 (live): Keycloak accepts an EXPIRED `id_token_hint` at the end-session endpoint without a confirmation page.
// Sign in, stay idle and offline past the id_token lifetime (the realm's accessTokenLifespan, 300 s), then sign out. The logout request the
// browser sends carries the hint; its `exp` (read from the payload, never printed) must be in the past, and the browser must still land
// on /login?signedout=idp, which signs in at once and shows the Keycloak form (the Keycloak session ended). A confirmation page would stop at Keycloak.
import { evidence, expect, keycloakUsername, login, test } from "./support";

const IDLE_MS = 5 * 60_000 + 15_000; // accessTokenLifespan 300 s + margin; the realm lifetime is not raised for tests

function hintExpiry(logoutUrl: string): number {
  const hint = new URL(logoutUrl).searchParams.get("id_token_hint") ?? "";
  const payload = JSON.parse(Buffer.from(hint.split(".")[1] ?? "", "base64url").toString("utf8")) as { exp?: number };
  if (typeof payload.exp !== "number") throw new Error("the logout request carries no id_token_hint with an exp claim");
  return payload.exp;
}

test("sign-out with an expired id_token_hint needs no Keycloak confirmation page", { tag: "@e2e" }, async ({ page, context }) => {
  test.setTimeout(IDLE_MS + 3 * 60_000);
  await login(page);
  const logout: { url?: string; status?: number } = {};
  page.on("response", (r) => {
    if (r.url().includes("/protocol/openid-connect/logout")) {
      logout.url = r.url();
      logout.status = r.status();
    }
  });
  // Idle and offline: the page's own polling would reach the app and refresh the tokens (a new id_token), so nothing may be sent.
  await context.setOffline(true);
  const idleUntil = Date.now() + IDLE_MS;
  await expect.poll(() => Date.now() >= idleUntil, { timeout: IDLE_MS + 30_000, intervals: [5_000] }).toBe(true);
  await context.setOffline(false);
  await page.getByRole("button", { name: /^Account:/ }).click();
  await page.getByRole("menu", { name: "Account" }).getByRole("menuitem", { name: "Sign out" }).click();
  await expect(
    keycloakUsername(page),
    "no Keycloak confirmation page: the browser comes straight back and the session ended",
  ).toBeVisible();
  const expiredSeconds = Math.floor(Date.now() / 1000) - hintExpiry(logout.url ?? "");
  expect(expiredSeconds, "the id_token_hint was already expired when it was sent").toBeGreaterThan(0);
  evidence({ event: "signout.expired_hint", logout_status: logout.status, hint_expired_seconds_ago: expiredSeconds });
});
