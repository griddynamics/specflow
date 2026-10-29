import { NextResponse, type NextRequest } from "next/server";
import { serverEnv, sessionCookieName } from "@/lib/config";
import { skipsLogin } from "@/lib/proxy-rules.mjs";

// Optimistic gate for pages only (docs: authentication.md "Optimistic checks with
// Proxy"): a missing session cookie redirects to /login. Real checks happen in
// the pages and in the BFF. The matcher MUST exclude /api: a proxy that matches
// an upload route buffers and silently truncates the body at 10 MiB
// (proxyClientMaxBodySize, SP-C).
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (pathname === "/login") return NextResponse.next();
  const env = serverEnv();
  if (skipsLogin(pathname)) return NextResponse.next(); // A3, FE-BP-06

  const name = sessionCookieName(env.PUBLIC_URL);
  const hasSession = request.cookies.getAll().some((c) => c.name === name || c.name.startsWith(`${name}.`));
  if (hasSession) return NextResponse.next();

  // Absolute URL on PUBLIC_URL: request.url can carry the internal bind address (0.0.0.0).
  return NextResponse.redirect(new URL("/login", env.PUBLIC_URL ?? request.url));
}

export const config = {
  matcher: ["/((?!api(?:/|$)|_next/|favicon\\.ico).*)"],
};
