// B-08, B-09 (R2.4; SPECS §7): sign-out ends the identity provider's session when it advertises an end_session_endpoint (the stub
// returns to `/login?signedout=idp`, which signs in again at once, as Keycloak would show its form), and still lands on
// `/login?signedout=1`, a card that waits, when it does not (the Google case). The stub issuer (scripts/serve-production.mjs) records every
// end-session call; the switch is the control port's `POST /oidc/end-session`. Never a token value is read here.
import type { Page } from "@playwright/test";
import { canvas, unique } from "./canvas-support";
import { expect, oidcObservations, resetOidc, setEndSession, test, v2Header } from "./support";

async function signOutFromMenu(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Account" }).click();
  await page.getByRole("menu", { name: "Account" }).getByRole("menuitem", { name: "Sign out" }).click();
}

test.describe("RP-initiated logout", { tag: "@production" }, () => {
  test.afterEach(async () => {
    await setEndSession(true);
  });

  test("B-08: Sign out makes exactly one successful end-session call with a valid id_token_hint, returns to /login?signedout=idp and signs in again", async ({
    page,
    baseURL,
  }) => {
    await setEndSession(true);
    // A project of its own, so the header (and its Account menu) is the canvas': with no project at all, `/` is the
    // un-closeable first-run window and there is no Account to press behind it.
    await canvas(page, unique("Logout end-session"));
    await resetOidc();
    await signOutFromMenu(page);
    // The stub issuer keeps no session, so the automatic sign-in that follows the return lands on the canvas again.
    await expect(v2Header(page)).toBeVisible({ timeout: 15_000 });
    await expect(page).not.toHaveURL(/\/login/);
    const seen = await oidcObservations();
    expect(seen.endSessionCalls, "one end-session call").toBe(1);
    expect(seen.endSessionRequests).toEqual([
      {
        hasIdTokenHint: true,
        idTokenHintValid: true,
        postLogoutRedirectUri: `${new URL(baseURL ?? "http://127.0.0.1:3260").origin}/login?signedout=idp`,
        clientId: "specflow-web",
        ok: true,
      },
    ]);
  });

  test("B-09: without an end_session_endpoint, Sign out still lands on /login?signedout=1 and no end-session call is made", async ({
    page,
  }) => {
    await setEndSession(false);
    await canvas(page, unique("Logout without endpoint"));
    await resetOidc();
    await signOutFromMenu(page);
    await expect(page).toHaveURL(/\/login\?signedout=1$/);
    await expect(page.getByTestId("login-signed-out")).toHaveText("You are signed out.");
    await expect(page.getByTestId("login-oidc")).toBeVisible();
    expect((await oidcObservations()).endSessionCalls, "nothing reached the issuer's logout").toBe(0);
  });
});
