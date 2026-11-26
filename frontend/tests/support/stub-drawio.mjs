// Stub of the diagrams.net link source (docs/sources/importers.md "diagrams.net link"): what a link is worth to the check and the import.
// Nothing is fetched. A host of diagrams.net or draw.io with `#R<payload>` (the diagram in the link) or `#U<url>` (a public file) is a diagram
// "Release flow" with 2 pages. Reserved payloads: `#Rbad` 422 drawio_not_a_diagram, `#Rbig` 413 drawio_too_large, `#Ufail` 409 drawio_fetch_failed,
// `#Rmany` a capped diagram. Storage hashes (`#G` Drive, `#H` GitHub, `#A` GitLab, `#W` OneDrive, `#T` Trello, `#L` browser, `#D` Dropbox): 422
// drawio_link_unsupported. Anything that is not an https link of those hosts: 422 drawio_link_invalid.
// Also read like the backend: `#P{json}` with a `data` member (a diagram, like `#R`), the legacy `?url=` and a bare https `.drawio` URL (like `#U`),
// http, a port other than 443 and a link over 256 KiB (413 drawio_too_large).
const HOSTS = new Set(["app.diagrams.net", "viewer.diagrams.net", "embed.diagrams.net", "www.draw.io", "app.draw.io"]);
const STORAGES = new Set(["G", "H", "A", "W", "T", "L", "D"]);
const MAX_LINK_BYTES = 256 * 1024;
const INVALID = { status: 422, code: "drawio_link_invalid" };

/** The reserved word of a payload (`bad`, `big`, `fail`, `many`), else "". */
function verdict(rest) {
  if (rest === "bad") return { status: 422, code: "drawio_not_a_diagram" };
  if (rest === "big") return { status: 413, code: "drawio_too_large" };
  if (rest === "fail") return { status: 409, code: "drawio_fetch_failed" };
  return {
    status: 200,
    body: {
      name: "Release flow",
      pages: [
        { name: "Main", shapes: 41, arrows: 41 },
        { name: "Details", shapes: 12, arrows: 9 },
      ],
      capped: rest === "many",
    },
  };
}

/** @returns {{status: number, code: string} | {status: 200, body: object}} */
export function checkDrawio(link) {
  const text = link.trim();
  if (Buffer.byteLength(text) > MAX_LINK_BYTES) return { status: 413, code: "drawio_too_large" };
  let url;
  try {
    url = new URL(text);
  } catch {
    return INVALID;
  }
  if (url.protocol !== "https:" || url.username !== "" || url.password !== "" || (url.port !== "" && url.port !== "443")) return INVALID;
  if (!HOSTS.has(url.hostname)) return url.pathname.toLowerCase().endsWith(".drawio") && url.hash === "" ? verdict("") : INVALID;
  const hash = url.hash.slice(1);
  const head = hash.charAt(0);
  if (STORAGES.has(head)) return { status: 422, code: "drawio_link_unsupported" };
  if (head === "R" || head === "U") return verdict(hash.slice(1));
  if (head === "P") {
    try {
      const data = JSON.parse(decodeURIComponent(hash.slice(1))).data;
      return typeof data === "string" && data !== "" ? verdict(data) : INVALID;
    } catch {
      return INVALID;
    }
  }
  if (url.searchParams.get("url") || url.pathname.toLowerCase().endsWith(".drawio")) return verdict("");
  return INVALID;
}
