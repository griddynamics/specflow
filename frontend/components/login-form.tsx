"use client";
// FE-BP-03.AC5 (TECH-SPECS §1.7; P17 s04): the sign-in controls of /login, restyled with the design-system TextField and Button.
// Behaviour and test hooks are unchanged from the hello app: the dev form exists only in AUTH_MODE=dev (AC-6.5) and is prefilled
// with LOCAL_TEST_USER_EMAIL; the OIDC mode shows one button. Both land on `/` after sign-in.
import { useState } from "react";
import { signIn } from "next-auth/react";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/TextField";

export function DevLoginForm({ defaultEmail, home }: Readonly<{ defaultEmail: string; home: string }>) {
  const [email, setEmail] = useState(defaultEmail);
  const [busy, setBusy] = useState(false);
  async function submit() {
    setBusy(true);
    await signIn("credentials", { email, redirectTo: home });
    setBusy(false);
  }
  return (
    <form
      className="login__form"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <TextField
        label="Email"
        type="email"
        name="email"
        autoComplete="email"
        required
        value={email}
        onChange={(e) => {
          setEmail(e.target.value);
        }}
        help="Local development sign-in: no password."
        data-testid="login-dev-email"
      />
      <Button type="submit" disabled={busy} arrow data-testid="login-dev-submit">
        Sign in
      </Button>
    </form>
  );
}

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
        Sign in with Google
      </Button>
    </div>
  );
}
