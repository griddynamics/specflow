"use client";
// FE-BP-03.AC3, AC4 (TECH-SPECS §1.7, O12): the production wrapper around the shared AppShell. It adds what only production has:
// the avatar menu item "Sign out" (the production provider has no reset) and the listener for
// UNAUTHENTICATED_EVENT, which `lib/api.ts` and the `/me` helper fire on a 401 from the BFF. Not a screen: the only import of
// `lib/api.ts` here is the event name.
//
// P17 fix1 (review M1, GAPS G-P17-4): both paths share one sign-out routine. The BFF route is wrapped in Auth.js `auth()`, which
// writes the session cookie on every response, so a BFF response still in flight could put the session back after the sign-out.
// The routine therefore (1) unmounts the screens first (Mission Control leaves: polling stops, nothing new starts), (2) signs out
// without a redirect, (3) waits for late responses and re-checks the session, signing out again while one is still present
// (at most SIGN_OUT_ATTEMPTS), then (4) replaces the page with `/login?signedout=1`. The flag tells /login not to bounce a
// still-valid session back to `/` (that would loop through another 401); it is never shown to the user as data.
import { getSession, signOut } from "next-auth/react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AppShell } from "@/components/shell/AppShell";
import type { MenuItem } from "@/components/ui/Menu";
import { Spinner } from "@/components/ui/ProgressBar";
import { UNAUTHENTICATED_EVENT } from "@/lib/api";
import { useRoutes } from "@/lib/data/provider";

export const SIGN_OUT_ATTEMPTS = 3;
const SETTLE_MS = 1_000; // time for a BFF response that was already in flight to land before the session is checked
export const SIGNED_OUT_PARAM = "signedout";

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function ProductionShell({ children }: Readonly<{ children: ReactNode }>) {
  const routes = useRoutes();
  const [signingOut, setSigningOut] = useState(false);
  const started = useRef(false);

  const endSession = useCallback(async () => {
    if (started.current) return; // several 401s at once, or a 401 during a menu sign-out: one routine only
    started.current = true;
    setSigningOut(true);
    await wait(0); // let React unmount the screens before the sign-out request leaves
    for (let attempt = 1; attempt <= SIGN_OUT_ATTEMPTS; attempt++) {
      await signOut({ redirect: false }).catch(() => undefined);
      await wait(SETTLE_MS);
      const session = await getSession().catch(() => null);
      if (!session) break;
    }
    window.location.replace(`${routes.login()}?${SIGNED_OUT_PARAM}=1`);
  }, [routes]);

  useEffect(() => {
    const onUnauthenticated = () => void endSession();
    window.addEventListener(UNAUTHENTICATED_EVENT, onUnauthenticated);
    return () => {
      window.removeEventListener(UNAUTHENTICATED_EVENT, onUnauthenticated);
    };
  }, [endSession]);

  const accountItems = useMemo<MenuItem[]>(
    () => [{ label: "Sign out", icon: "sign-out", onSelect: () => void endSession() }],
    [endSession],
  );

  if (signingOut) {
    return (
      <div className="signing-out" data-testid="signing-out">
        <Spinner size={24} label="Signing out" />
      </div>
    );
  }
  return <AppShell accountItems={accountItems}>{children}</AppShell>;
}
