// FE-BP-03.AC5, FE-BP-06, A3 (TECH-SPECS §1.7, §8): /login, outside the session gate of `(session)` (the proxy lets this path through).
// The decision is `loginMode` (lib/login-mode.ts): a signed-in person goes to `/`; a signed-out one is sent straight to the identity
// provider; a failed sign-in, a local-only sign-out and a session that outlived a sign-out show the card instead. The error parameter is
// never echoed (review m3), and `signedout` is only a flag (G-P17-4), never shown as data.
import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { LoginCard } from "@/components/v2/login/LoginCard";
import { loginMode } from "@/lib/login-mode";
import { routesFor } from "@/lib/routes";

type Query = Record<string, string | string[] | undefined>;

export default async function LoginPage({ searchParams }: Readonly<{ searchParams: Promise<Query> }>) {
  const home = routesFor("production").home();
  const query = await searchParams;
  const flag = query.signedout;
  const mode = loginMode({
    session: (await auth()) !== null,
    signedout: typeof flag === "string" ? flag : undefined,
    error: query.error !== undefined,
  });
  if (mode === "home") redirect(home);
  return <LoginCard mode={mode} home={home} />;
}
