import { SignJWT } from "jose";

export const DEV_TOKEN_TTL_SECONDS = 8 * 3600;

/** Dev token, architecture-notes 3.6: HS256, claims as the backend DevTokenVerifier expects. */
export async function mintDevToken(email: string, signingKey: string) {
  const iat = Math.floor(Date.now() / 1000);
  const exp = iat + DEV_TOKEN_TTL_SECONDS;
  const token = await new SignJWT({
    azp: "specflow-web",
    typ: "Bearer",
    email,
    email_verified: true,
    name: email.split("@")[0],
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuer("specflow-dev-login")
    .setAudience("specflow-api")
    .setSubject(`dev:${email}`)
    .setIssuedAt(iat)
    .setExpirationTime(exp)
    .sign(new TextEncoder().encode(signingKey));
  return { token, exp };
}
