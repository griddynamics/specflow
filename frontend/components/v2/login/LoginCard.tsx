// FE-BP-03.AC5 (decision "skip the sign-in page"): the card of /login in the Graphico style. A signed-out visitor normally never sees it:
// the sign-in starts at once and the card only says where the person is going (`auto`). It stays for what needs a person: a failed sign-in,
// a local-only sign-out and a session that survived a sign-out. The heading is the wordmark; the page has no <h1> that repeats the tab.
import type { ReactNode } from "react";
import { Brand, Card, GraphicoRoot, Notice } from "@graphico/ui";
import { V2_COPY } from "../v2-copy";
import { OidcSignIn } from "./OidcSignIn";
import "./login.css";

export type LoginCardMode = "auto" | "error" | "signed-out" | "still-signed-in";

function message(mode: LoginCardMode): ReactNode {
  switch (mode) {
    case "auto":
      return (
        <div data-testid="login-redirecting">
          <Notice>{V2_COPY.takingToSignIn}</Notice>
        </div>
      );
    case "error":
      return (
        <div data-testid="login-error">
          <Notice role="alert">{V2_COPY.signInFailed}</Notice>
        </div>
      );
    case "signed-out":
      return (
        <div data-testid="login-signed-out">
          <Notice>{V2_COPY.signedOut}</Notice>
        </div>
      );
    case "still-signed-in":
      return (
        <div data-testid="login-still-signed-in">
          <Notice role="alert">{V2_COPY.stillSignedIn}</Notice>
        </div>
      );
  }
}

export function LoginCard({ mode, home }: Readonly<{ mode: LoginCardMode; home: string }>) {
  return (
    <GraphicoRoot>
      <main className="v2-login" data-testid="login-page">
        <Card className="v2-login__card">
          <Brand />
          {message(mode)}
          <OidcSignIn home={home} auto={mode === "auto"} />
        </Card>
      </main>
    </GraphicoRoot>
  );
}
