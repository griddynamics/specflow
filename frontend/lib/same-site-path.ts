// The one rule for a path the app may redirect to (OAuth return routes, the Integrations `next`). Pure: no Node, no React.

/**
 * A same-site path: starts with `/`, not `//`, no backslash, no whitespace or control character, ASCII only; at most 2048 characters.
 * Its path part has no `.` or `..` segment (`%2e` counts as `.`) and no encoded `/` or `\` (`%2f`, `%5c`): a URL parser resolves
 * `/.//evil.com` to `//evil.com`, another host. The API checks `return_to` the same way.
 */
export function isSameSitePath(r: unknown): r is string {
  if (typeof r !== "string" || r.length === 0 || r.length > 2048) return false;
  if (!r.startsWith("/") || r.startsWith("//") || r.includes("\\")) return false;
  for (const ch of r) {
    const c = ch.codePointAt(0) ?? 0;
    if (c <= 0x20 || c >= 0x7f) return false;
  }
  const beforeQuery = r.split(/[?#]/, 1)[0].toLowerCase();
  if (beforeQuery.includes("%2f") || beforeQuery.includes("%5c")) return false;
  return !beforeQuery.split("/").some((segment) => {
    const s = segment.replaceAll("%2e", ".");
    return s === "." || s === "..";
  });
}
