// B-16, B-08 (live; R2.2, R2.4): the live pack signs in as alice through the real Keycloak form, and "Sign out" ends the Keycloak SSO
// session too: the next "Sign in" shows the Keycloak form again instead of signing alice in silently. No model calls, no project.
import { evidence, expect, KEYCLOAK_USERNAME_FIELD, login, signOut, test } from "./support";

test("alice signs in through Keycloak; sign-out ends the Keycloak session", { tag: "@e2e" }, async ({ page }) => {
  await test.step("sign in through the Keycloak form", async () => {
    await login(page);
    await expect(page.getByRole("button", { name: /^Account: Alice/ }), "the signed-in user is alice").toBeVisible();
  });

  await test.step("sign out lands on /login?signedout=1 and the app session is gone", async () => {
    await signOut(page);
    await expect(page.getByTestId("login-oidc")).toBeVisible();
    await page.goto("/");
    await expect(page).toHaveURL(/\/login$/);
  });

  await test.step("the Keycloak session ended: signing in again shows the form", async () => {
    await page.getByTestId("login-oidc").click();
    await expect(page.locator(KEYCLOAK_USERNAME_FIELD), "Keycloak asks for credentials again (no silent re-auth)").toBeVisible();
    await expect(page.getByTestId("app-shell")).toHaveCount(0);
    evidence({ event: "signout.keycloak_session_ended", keycloak_form_shown: true });
  });
});
