// B-08, B-09 (R2.4; SPECS §7): sign-out ends the identity provider's session when it advertises an end_session_endpoint, and still
// lands on `/login?signedout=1` when it does not (the Google case). The stub issuer (scripts/serve-production.mjs) records every
// end-session call; the switch is the control port's `POST /oidc/end-session`. Never a token value is read here.
import type { Page } from "@playwright/test";
import { expect, login, oidcObservations, resetOidc, setEndSession, test } from "./support";

async function signOutFromMenu(page: Page): Promise<void> {
  await page.getByRole("button", { name: /^Account:/ }).click();
  await page.getByRole("menu", { name: "Account" }).getByRole("menuitem", { name: "Sign out" }).click();
}

test.describe("RP-initiated logout", { tag: "@production" }, () => {
  test.afterEach(async () => {
    await setEndSession(true);
  });

  test("B-08: Sign out lands on /login?signedout=1 after exactly one successful end-session call with a valid id_token_hint", async ({
    page,
    baseURL,
  }) => {
    await setEndSession(true);
    await login(page);
    await resetOidc();
    await signOutFromMenu(page);
    await expect(page).toHaveURL(/\/login\?signedout=1$/);
    await expect(page.getByTestId("login-oidc")).toBeVisible();
    const seen = await oidcObservations();
    expect(seen.endSessionCalls, "one end-session call").toBe(1);
    expect(seen.endSessionRequests).toEqual([
      {
        hasIdTokenHint: true,
        idTokenHintValid: true,
        postLogoutRedirectUri: `${new URL(baseURL ?? "http://127.0.0.1:3260").origin}/login?signedout=1`,
        clientId: "specflow-web",
        ok: true,
      },
    ]);
    await page.goto("/");
    await expect(page).toHaveURL(/\/login$/); // the app session is gone too
  });

  test("B-09: without an end_session_endpoint, Sign out still lands on /login?signedout=1 and no end-session call is made", async ({
    page,
  }) => {
    await setEndSession(false);
    await login(page);
    await resetOidc();
    await signOutFromMenu(page);
    await expect(page).toHaveURL(/\/login\?signedout=1$/);
    await expect(page.getByTestId("login-oidc")).toBeVisible();
    expect((await oidcObservations()).endSessionCalls, "nothing reached the issuer's logout").toBe(0);
    await page.goto("/");
    await expect(page).toHaveURL(/\/login$/);
  });
});
