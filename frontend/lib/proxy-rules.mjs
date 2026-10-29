// A3, FE-BP-06 (TECH-SPECS §8): which paths skip the login redirect. Pure and plain .mjs (like env-rules.mjs), so
// `node --test` imports it without a loader. The proxy matcher in proxy.ts stays untouched (R12).

export const ALWAYS_PUBLIC = ["/ds", "/icon.svg", "/favicon.ico", "/apple-icon.png"]; // A3 + G01-12 + D22: no client content
const under = (path, prefix) => path === prefix || path.startsWith(prefix + "/");

/**
 * A3, FE-BP-06: true = let the request through without a session.
 * @param {string} pathname
 * @returns {boolean}
 */
export function skipsLogin(pathname) {
  return ALWAYS_PUBLIC.some((p) => under(pathname, p));
}
