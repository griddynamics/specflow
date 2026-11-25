"use client";
// FE-BP-03.AC3, AC4 (TECH-SPECS §1.7, O12): the sign-out routine every signed-in tree shares (the V1 shell `app/(app)/production-shell.tsx`
// and the V2 shell `components/v2/shell/V2Shell.tsx`). It owns what only a signed-in tree has: the listener for UNAUTHENTICATED_EVENT,
// which `lib/api.ts` and the `/me` helper fire on a 401 from the BFF, and the menu's "Sign out". Not a screen: the only imports of
// `lib/api.ts` here are the event name and the end-session call.
//
// P17 fix1 (review M1, GAPS G-P17-4): both paths share one sign-out routine. The BFF route is wrapped in Auth.js `auth()`, which
// writes the session cookie on every response, so a BFF response still in flight could put the session back after the sign-out.
// The routine therefore (1) unmounts the screens first (the caller renders nothing while `signingOut`: polling stops, nothing new
// starts), (2) signs out without a redirect, (3) waits for late responses and re-checks the session, signing out again while one is
// still present (at most SIGN_OUT_ATTEMPTS), then (4) replaces the page with the sign-out destination. That is the identity provider's
// logout page (RP-initiated logout, B-08) when the issuer advertises one, which returns to `/login?signedout=1`; otherwise
// `/login?signedout=1` itself (B-09). The destination is asked from the server BEFORE the first sign-out: it needs the id_token in the
// session cookie. The flag tells /login not to bounce a still-valid session back to `/` (that would loop through another 401); it is
// never shown to the user as data.
import { getSession, signOut } from "next-auth/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { endSessionDestination, UNAUTHENTICATED_EVENT } from "@/lib/api";
import { useRoutes } from "@/lib/data/provider";

const SIGN_OUT_ATTEMPTS = 3;
const SETTLE_MS = 1_000; // time for a BFF response that was already in flight to land before the session is checked
const SIGNED_OUT_PARAM = "signedout";

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** `signingOut`: the routine runs (render no screen); `endSession`: start it (a 401 does the same through the event). */
export function useSignOut(): { signingOut: boolean; endSession: () => Promise<void> } {
  const routes = useRoutes();
  const [signingOut, setSigningOut] = useState(false);
  const started = useRef(false);

  const endSession = useCallback(async () => {
    if (started.current) return; // several 401s at once, or a 401 during a menu sign-out: one routine only
    started.current = true;
    setSigningOut(true);
    await wait(0); // let React unmount the screens before the sign-out request leaves
    const fallback = `${routes.login()}?${SIGNED_OUT_PARAM}=1`;
    const destination = await endSessionDestination();
    for (let attempt = 1; attempt <= SIGN_OUT_ATTEMPTS; attempt++) {
      await signOut({ redirect: false }).catch(() => undefined);
      await wait(SETTLE_MS);
      const session = await getSession().catch(() => null);
      if (!session) break;
    }
    window.location.replace(typeof destination === "string" && destination !== "" ? destination : fallback);
  }, [routes]);

  useEffect(() => {
    const onUnauthenticated = () => void endSession();
    window.addEventListener(UNAUTHENTICATED_EVENT, onUnauthenticated);
    return () => {
      window.removeEventListener(UNAUTHENTICATED_EVENT, onUnauthenticated);
    };
  }, [endSession]);

  return { signingOut, endSession };
}
