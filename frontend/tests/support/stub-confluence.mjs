// Stub of the Confluence link source (docs/sources/importers.md "Confluence link", docs/mission-control/integrations-api.md): what a link
// is worth to the check and to the import. Nothing calls Atlassian. The bound site is `northwind.atlassian.test`. Reserved links:
//   /wiki/x/<id>                 a short link: 422 confluence_short_link_unsupported
//   .../pages/404, .../pages/403 409 confluence_not_found, 409 confluence_forbidden
//   .../pages/<other>            a page "Checkout notes", 4 pages (with follow: 3 linked Jira issues)
//   /wiki/spaces/BIG             a space "Big space", 100 pages, capped
//   /wiki/spaces/<other>         a space "Ordering", 42 pages
//   another host                 422 atlassian_site_mismatch; anything else: 422 confluence_link_invalid
export const SITE_HOST = "northwind.atlassian.test"; // a reserved host: no real domain is ever named
const MAX_PAGES = 100;
const LINKED_ISSUES = 3;

/** @returns {{status: number, code: string} | {status: 200, body: object}} */
export function checkConfluence(link, follow) {
  let url;
  try {
    url = new URL(link);
  } catch {
    return { status: 422, code: "confluence_link_invalid" };
  }
  if (url.protocol !== "https:" || url.username !== "") return { status: 422, code: "confluence_link_invalid" };
  if (url.hostname !== SITE_HOST) return { status: 422, code: "atlassian_site_mismatch" };
  if (/^\/wiki\/x\//.test(url.pathname)) return { status: 422, code: "confluence_short_link_unsupported" };
  const page = /^\/wiki\/spaces\/[^/]+\/pages\/(\d+)/.exec(url.pathname)?.[1];
  if (page === "404") return { status: 409, code: "confluence_not_found" };
  if (page === "403") return { status: 409, code: "confluence_forbidden" };
  if (page !== undefined) {
    return {
      status: 200,
      body: { kind: "page", title: "Checkout notes", pages: 4, capped: false, linked_issues: follow ? LINKED_ISSUES : 0 },
    };
  }
  const space = /^\/wiki\/spaces\/([^/]+)\/?$/.exec(url.pathname)?.[1];
  if (space === undefined) return { status: 422, code: "confluence_link_invalid" };
  const big = space === "BIG";
  return {
    status: 200,
    body: { kind: "space", title: big ? "Big space" : "Ordering", pages: big ? MAX_PAGES : 42, capped: big, linked_issues: 0 },
  };
}
