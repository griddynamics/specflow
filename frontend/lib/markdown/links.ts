// FE-MC-08, FE-MC-05, FE-DEC-03.AC7, FE-BOT-02.AC4 (TECH-SPECS §1.4): document links inside Markdown and the Files deep link helper.
// Pure: no React, no fetch. `resolveDocHref` is the rule; `filesLink` is the ONE way later phases (DocLinks P12, Citations P13)
// build a link to a file and section. It sits on top of `routes.file` (lib/routes.ts is frozen), so demo and production trees
// both work.
import type { DocRef } from "@/lib/domain/common";
import type { RouteBuilder } from "@/lib/routes";

const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

function decode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/**
 * Resolves a Markdown link target against the document that contains it (TECH-SPECS §1.4).
 * - `#anchor` -> the same document at that anchor.
 * - `a/b.md`, `./b.md`, `../x/y.md#anchor` -> a document path relative to the current document's folder (no leading slash).
 * - null for everything else: other schemes (`https:`, `mailto:`, `javascript:`), absolute paths, `//host`, non-Markdown files,
 *   and a path that climbs above the project root.
 */
export function resolveDocHref(fromFile: string, href: string | undefined | null): DocRef | null {
  if (href === undefined || href === null || href === "") return null;
  const h = href.trim();
  if (h === "" || SCHEME.test(h) || h.startsWith("/") || h.startsWith("\\")) return null;
  const hash = h.indexOf("#");
  const rawRef = hash < 0 ? h : h.slice(0, hash);
  const anchor = hash < 0 ? "" : decode(h.slice(hash + 1));
  if (rawRef === "") return anchor ? { path: fromFile, anchor } : null;
  const path = joinSegments(fromFile, rawRef.split("?")[0]);
  if (path === null || !/\.md$/i.test(path)) return null;
  return anchor ? { path, anchor } : { path };
}

/** The path `target` names from the folder of `fromFile`; null for an encoded separator or a path above the project root. */
function joinSegments(fromFile: string, target: string): string | null {
  const parts = fromFile.split("/").slice(0, -1);
  for (const seg of target.split("/")) {
    const s = decode(seg);
    if (s.includes("/") || s.includes("\\")) return null; // an encoded separator (`..%2f`) is never a path step
    if (s === "" || s === ".") continue;
    if (s === "..") {
      if (parts.length === 0) return null; // above the project root
      parts.pop();
    } else parts.push(s);
  }
  return parts.join("/");
}

/**
 * The URL of a file (and optional section) in the Files view of a project: `…/mission-control?view=files&file=<path>&section=<anchor>`.
 * Use it for every link into the Files view (DocLinks, Citations, Markdown links). Pass `useRoutes()` from the provider.
 */
export function filesLink(routes: Pick<RouteBuilder, "file">, projectId: string, file: string, section?: string | null): string {
  return routes.file(
    projectId,
    section !== undefined && section !== null && section !== "" ? { path: file, anchor: section } : { path: file },
  );
}

/** Same as `filesLink` for a `DocRef` (MOCKS `path#anchor` references stored in fixtures). */
export function docRefLink(routes: Pick<RouteBuilder, "file">, projectId: string, ref: DocRef): string {
  return filesLink(routes, projectId, ref.path, ref.anchor);
}
