// FE-BP-03.AC5, FE-BP-06, A3 (TECH-SPECS §1.7, §8; P17 s04): /login on the design system. Same route and modes as the hello page:
// a signed-in user goes to `/`; AUTH_MODE=dev shows the e-mail form (AC-6.5), OIDC one button. The logo is an asset under `/ds/`, which
// loads without a session (lib/proxy-rules.mjs), and the page has no shell.
// P17 fix1: `?error=` (Auth.js sends a failed sign-in back here) shows one fixed message, never the parameter's value (review m3).
// `?signedout=1` comes from the production sign-out routine: if a session is still present then (GAPS G-P17-4), the page does not
// bounce it back to `/` (that could loop through another 401); it says so instead (review M1). A normal sign-out shows no note (D21).
import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { DevLoginForm, OidcLoginButton } from "@/components/login-form";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { ThemeLogo } from "@/components/ui/ThemeLogo";
import { PageTitle } from "@/components/ui/TopBar";
import { appConfig } from "@/lib/config";
import { routesFor } from "@/lib/routes";
import "./login.css";

const LOGIN_ERROR = "Sign-in did not work. Try again.";
const STILL_SIGNED_IN = "You are still signed in: the session could not be ended. Close the browser window to end it, or sign in again.";

type Query = Record<string, string | string[] | undefined>;

export default async function LoginPage({ searchParams }: Readonly<{ searchParams: Promise<Query> }>) {
  const routes = routesFor("production");
  const query = await searchParams;
  const signedOut = query.signedout === "1";
  const failed = query.error !== undefined;
  const session = await auth();
  if (session && !signedOut) redirect(routes.home());
  const cfg = appConfig();
  return (
    <div className="gdg-root body">
      <main className="login gdg-scene" data-testid="login-page">
        <GlassSurface variant="strong" className="login__card">
          <div className="login__brand" data-testid="login-logo">
            <ThemeLogo
              className="login__logo"
              light={routes.asset("Logos/full-color-black-horizontal.svg")}
              dark={routes.asset("Logos/full-color-white-horizontal.svg")}
              alt="Grid Dynamics"
            />
          </div>
          <PageTitle className="title-2 login__title">Specflow</PageTitle>
          {failed ? (
            <p className="body login__alert" role="alert" data-testid="login-error">
              {LOGIN_ERROR}
            </p>
          ) : null}
          {signedOut && session ? (
            <p className="body login__note" role="alert" data-testid="login-still-signed-in">
              {STILL_SIGNED_IN}
            </p>
          ) : null}
          {/* The dev form exists only in dev mode (AC-6.5). */}
          {cfg.authMode === "dev" ? (
            <DevLoginForm defaultEmail={cfg.localTestUserEmail} home={routes.home()} />
          ) : (
            <OidcLoginButton home={routes.home()} />
          )}
        </GlassSurface>
      </main>
    </div>
  );
}
