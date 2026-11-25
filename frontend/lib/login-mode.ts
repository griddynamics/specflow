// What /login does for one request (pure, so the decision table is unit tested without Next):
//   home            a session and no sign-out flag: go to `/`.
//   auto            no session, no error, and not a plain local sign-out (`signedout=1`): start the OIDC sign-in at once. That is a signed-out
//                   URL that the proxy sent here, and the return from the identity provider's logout page (`signedout=idp`: its session is
//                   ended, so the person sees the provider's form straight away).
//   error           Auth.js sent a failed sign-in back (`?error=`): a fixed message and a button, never an automatic redirect (no loop).
//   signed-out      a local-only sign-out (`signedout=1`, an issuer with no end-session endpoint such as Google): say so and wait, since an
//                   automatic sign-in would silently sign the person back in there.
//   still-signed-in a sign-out flag with a session still present (GAPS G-P17-4): say so; bouncing it to `/` could loop through a 401.
export type LoginMode = "home" | "auto" | "error" | "signed-out" | "still-signed-in";

export interface LoginInput {
  session: boolean;
  /** The raw `signedout` query value: `"1"` (local), `"idp"` (back from the provider's logout), anything else is no flag. */
  signedout: string | undefined;
  error: boolean;
}

export function loginMode({ session, signedout, error }: LoginInput): LoginMode {
  const flagged = signedout === "1" || signedout === "idp";
  if (session) return flagged ? "still-signed-in" : "home";
  if (error) return "error";
  return signedout === "1" ? "signed-out" : "auto";
}
