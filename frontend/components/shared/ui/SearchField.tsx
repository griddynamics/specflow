// FE-DS-02 (R5): the one matcher of client-side search, from `SearchField` in bundle.js. The component is gone with the
// legacy tree; the matcher stays — the GitHub tab's repository picker matches its list client-side.

/**
 * The one matcher of client-side search: `query` against `haystack`, ignoring letter case, anywhere in the text or only at its start.
 * Diacritics are not folded (the matching of this app, unchanged).
 */
export function matchesQuery(haystack: string, query: string, at: "anywhere" | "start" = "anywhere"): boolean {
  const text = haystack.toLowerCase();
  const wanted = query.toLowerCase();
  return at === "start" ? text.startsWith(wanted) : text.includes(wanted);
}
