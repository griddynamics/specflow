// DESIGN "Safety and failure rules" (pasted URLs) and D10 (add is idempotent per canonical target): a pasted link is parsed into
// ids, never fetched. GitHub: github.com only. Drive (DATA-GDUI-0002; the same rules as backend drive.links): a folder
// only, exact lower-case host drive.google.com, https, no `#` after the id path. Pure; the backend does the same on the server.
import type { SourceLink } from "../sources";

export type ParsedLink =
  | { ok: true; kind: "github"; key: string; owner: string; repo: string }
  | { ok: true; kind: "gdrive"; key: string; id: string }
  | { ok: false; kind: SourceLink["kind"]; key: string };

const GITHUB = /^https:\/\/(?:www\.)?github\.com\/([A-Z0-9][A-Z0-9-]{0,38})\/([\w.-]{1,100}?)(?:\.git)?(?:[/?#].*)?$/i;

// DATA-GDUI-0002: ID = 10 or more of A-Z a-z 0-9 _ -; case-sensitive; a query string or a trailing "/" does not change the result.
const DRIVE_ID = /^[\w-]{10,}$/;
const DRIVE_FOLDER_FORMS = [
  /^https:\/\/drive\.google\.com\/drive\/folders\/([\w-]{10,})\/?$/,
  /^https:\/\/drive\.google\.com\/drive\/u\/\d+\/folders\/([\w-]{10,})\/?$/,
];

/** The folder id of a pasted Drive link (DATA-GDUI-0002), or null. The link is never fetched. */
export function parseDriveLink(text: string): string | null {
  const link = text.trim();
  if (DRIVE_ID.test(link)) return link;
  const q = link.indexOf("?");
  const base = q < 0 ? link : link.slice(0, q);
  const query = q < 0 ? "" : link.slice(q + 1);
  for (const form of DRIVE_FOLDER_FORMS) {
    const m = form.exec(base);
    if (m) return m[1];
  }
  if (base === "https://drive.google.com/open") {
    const id = new URLSearchParams(query).get("id");
    if (id !== null && DRIVE_ID.test(id)) return id;
  }
  return null;
}

/** Canonical target of a pasted link; `ok: false` = not a link of that kind (the card shows "unsupported link"). */
export function parseSourceLink(link: SourceLink): ParsedLink {
  const url = link.url.trim();
  if (link.kind === "github") {
    const m = GITHUB.exec(url);
    if (!m || m[2] === "." || m[2] === "..") return { ok: false, kind: "github", key: url.toLowerCase() };
    return { ok: true, kind: "github", key: `${m[1]}/${m[2]}`.toLowerCase(), owner: m[1], repo: m[2] };
  }
  const id = parseDriveLink(url);
  return id !== null && id !== "" ? { ok: true, kind: "gdrive", key: id, id } : { ok: false, kind: "gdrive", key: url };
}
