import NextAuth, { customFetch, type Account, type NextAuthConfig } from "next-auth";
import { encode as defaultEncode } from "next-auth/jwt";
import Credentials from "next-auth/providers/credentials";
import { appConfig, sessionCookieName, type AppConfig } from "@/lib/config";
import { mintDevToken } from "@/lib/dev-token";
import { backChannelFetch, needsRefresh, refreshSingleFlight } from "@/lib/oidc-refresh";

const SESSION_MAX_AGE = 8 * 3600;
const COOKIE_CHUNK_BYTES = 3933; // Auth.js splits a session cookie above this size
const chunkLogged = new Set<string>();

function log(event: string, fields: Record<string, unknown>) {
  // Never tokens, cookies or env values here.
  process.stdout.write(`${JSON.stringify({ event, ...fields })}\n`);
}

/** A sign-in call carries both the provider account and the user; later session reads carry neither. */
function isSignIn(account: Account | null | undefined, user: unknown): account is Account {
  return account !== null && account !== undefined && user !== null && user !== undefined;
}

function oidcProvider(cfg: AppConfig): NextAuthConfig["providers"][number] {
  return {
    id: "oidc",
    name: "Google",
    type: "oidc",
    issuer: cfg.oidc.issuer,
    clientId: cfg.oidc.clientId,
    clientSecret: cfg.oidc.clientSecret,
    checks: ["pkce", "state", "nonce"],
    authorization: { params: { scope: "openid email profile" } },
    [customFetch]: backChannelFetch,
  };
}

function devProvider(cfg: AppConfig): NextAuthConfig["providers"][number] {
  return Credentials({
    id: "credentials",
    name: "Dev login",
    credentials: { email: { label: "Email", type: "email" } },
    async authorize(credentials) {
      const submitted: unknown = credentials.email;
      const email = (typeof submitted === "string" ? submitted : "").trim().toLowerCase();
      if (!/^[^@\s]+@[^\s@][^\s.@]*\.[^\s@]+$/.test(email)) return null;
      const { token, exp } = await mintDevToken(email, cfg.devSigningKey);
      return {
        id: `dev:${email}`,
        email,
        name: email.split("@")[0],
        accessToken: token,
        accessExpiresAt: exp,
      };
    },
  });
}

function buildConfig(): NextAuthConfig {
  const cfg = appConfig(); // throws on an invalid env: fail closed (AC-6.4)
  const dev = cfg.authMode === "dev";
  if (dev) log("auth.mode.dev_enabled", {});
  const cookieName = sessionCookieName(cfg.publicUrl);

  return {
    providers: [dev ? devProvider(cfg) : oidcProvider(cfg)],
    secret: cfg.sessionSecret,
    // Needed because Auth.js has no AUTH_URL here; the request it sees is already pinned
    // to PUBLIC_URL by app/api/auth/[...nextauth]/route.ts (setting AUTH_URL would need a
    // NextRequest, which rewrites 127.0.0.1 to localhost).
    trustHost: true,
    useSecureCookies: cfg.secure,
    session: { strategy: "jwt", maxAge: SESSION_MAX_AGE },
    pages: { signIn: "/login" },
    cookies: {
      sessionToken: {
        name: cookieName,
        options: { httpOnly: true, sameSite: "lax", path: "/", secure: cfg.secure },
      },
    },
    jwt: {
      async encode(params) {
        const value = await defaultEncode(params);
        const sub = params.token?.sub ?? "";
        if (value.length > COOKIE_CHUNK_BYTES && sub !== "" && !chunkLogged.has(sub)) {
          if (chunkLogged.size > 1000) chunkLogged.clear();
          chunkLogged.add(sub);
          log("auth.session.cookie_chunked", {
            bytes: value.length,
            chunks: Math.ceil(value.length / COOKIE_CHUNK_BYTES),
          });
        }
        return value;
      },
    },
    callbacks: {
      async jwt({ token, user, account }) {
        if (isSignIn(account, user)) {
          // Sign-in: keep only what the BFF needs; no picture, no id_token.
          const base = { sub: token.sub, name: token.name, email: token.email };
          if (dev) return { ...base, access_token: user.accessToken, expires_at: user.accessExpiresAt };
          return {
            ...base,
            access_token: account.access_token,
            refresh_token: account.refresh_token,
            expires_at: account.expires_at,
          };
        }
        if (dev) {
          // No refresh for the dev token: it simply ends the session.
          return typeof token.expires_at === "number" && token.expires_at > 0 && Date.now() < token.expires_at * 1000 ? token : null;
        }
        if (!needsRefresh(token.expires_at)) return token;
        if (token.refresh_token === undefined || token.refresh_token === "") return null;
        const result = await refreshSingleFlight(token.refresh_token); // AC-6.8
        // A failed refresh clears the session (null removes the cookie).
        return result.ok ? { ...token, ...result.tokens } : null;
      },
      session({ session, token }) {
        // AC-6.7: only name and email reach the browser.
        return { ...session, user: { name: token.name, email: token.email } };
      },
    },
  };
}

let config: NextAuthConfig | undefined;

// Lazy: the config (and so the env check) is built per process on first request,
// never at `next build`.
export const { handlers, auth } = NextAuth(() => (config ??= buildConfig()));
