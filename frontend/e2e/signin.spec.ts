// B-16, B-08 (live; R2.2, R2.4): the live pack signs in as alice through the real Keycloak form, and "Sign out" ends the Keycloak SSO
// session too: the next "Sign in" shows the Keycloak form again instead of signing alice in silently. No model calls, no project.
import { evidence, expect, keycloakUsername, login, signOut, test } from "./support";

test("alice signs in through Keycloak; sign-out ends the Keycloak session", { tag: "@e2e" }, async ({ page }) => {
  await test.step("sign in through the Keycloak form", async () => {
    await login(page);
    await expect(page.getByRole("button", { name: /^Account: Alice/ }), "the signed-in user is alice").toBeVisible();
  });

  await test.step("sign out returns to /login?signedout=idp, which signs in at once: Keycloak asks for credentials again", async () => {
    await signOut(page);
    await expect(keycloakUsername(page), "Keycloak asks for credentials again (no silent re-auth)").toBeVisible();
    await expect(page.getByTestId("app-shell")).toHaveCount(0);
    evidence({ event: "signout.keycloak_session_ended", keycloak_form_shown: true });
  });
});
