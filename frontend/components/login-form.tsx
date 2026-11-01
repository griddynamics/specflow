"use client";
// FE-BP-03.AC5 (TECH-SPECS §1.7; P17 s04): the sign-in control of /login, on the design-system Button. One neutral button: the identity
// provider is whatever OIDC_ISSUER points at (Keycloak locally, Google or another issuer elsewhere). It lands on `/` after sign-in.
import { signIn } from "next-auth/react";
import { Button } from "@/components/ui/Button";

export function OidcLoginButton({ home }: Readonly<{ home: string }>) {
  return (
    <div className="login__form">
      <Button
        data-testid="login-oidc"
        arrow
        onClick={() => {
          void signIn("oidc", { redirectTo: home });
        }}
      >
        Sign in
      </Button>
    </div>
  );
}
