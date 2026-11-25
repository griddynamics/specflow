"use client";
// The sign-in control of /login (FE-BP-03.AC5): one neutral button, since the identity provider is whatever OIDC_ISSUER points at (Keycloak
// locally, Google or another issuer elsewhere). With `auto` it also starts the sign-in once on mount, so a person never has to click; the
// button stays as the fallback for when the redirect does not happen. The ref keeps React strict mode (effects run twice in development)
// from starting two sign-ins.
import { useEffect, useRef } from "react";
import { signIn } from "next-auth/react";
import { Button } from "@graphico/ui";
import { V2_COPY } from "../v2-copy";

export function OidcSignIn({ home, auto }: Readonly<{ home: string; auto: boolean }>) {
  const started = useRef(false);
  useEffect(() => {
    if (!auto || started.current) return;
    started.current = true;
    void signIn("oidc", { redirectTo: home });
  }, [auto, home]);
  return (
    <Button
      className="v2-login__button"
      data-testid="login-oidc"
      onClick={() => {
        void signIn("oidc", { redirectTo: home });
      }}
    >
      {V2_COPY.signIn}
    </Button>
  );
}
