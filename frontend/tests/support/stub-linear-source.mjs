// Stub of the Linear source (docs/sources/importers.md "Linear", docs/mission-control/integrations-api.md): what a link or identifier is worth to the
// check and the import. Nothing calls Linear. Reserved values:
//   GRA-12   an issue "Checkout redesign" with 6 sub-issues (GRA-101..GRA-106)      .../project/checkout-0a1b2c3d4e5f   a project "Checkout" with 18 issues and 2 documents
//   .../project/everything-0a1b2c3d4e60   a project of 250 issues (over the cap)      .../project/empty-0a1b2c3d4e61   a project with nothing in it
//   GRA-404 409 linear_not_found    GRA-403 409 linear_forbidden    GRA-401 409 linear_reconnect_required    GRA-429 429 linear_rate_limited
//   anything else that is not an identifier or a linear.app project or issue link: 422 linear_link_invalid
const IDENTIFIER = /^([A-Z][A-Z0-9]*-\d+)$/;
const ISSUE_CAP = 200;
const PROJECTS = { checkout: ["Checkout", 18, 2], everything: ["Everything", 250, 0], empty: ["Empty", 0, 0] };
const REFUSED = {
  "GRA-404": { status: 409, code: "linear_not_found" },
  "GRA-403": { status: 409, code: "linear_forbidden" },
  "GRA-401": { status: 409, code: "linear_reconnect_required" },
  "GRA-429": { status: 429, code: "linear_rate_limited" },
};
const STATES = ["Backlog", "Todo", "In Progress", "Done"];
const WORDS = [
  "Cart totals",
  "Address form",
  "Payment retry",
  "Promo codes",
  "Order summary",
  "Guest checkout",
  "Tax lines",
  "Receipt email",
  "Saved cards",
];

/** The issues of a target as the check lists them: in order, at most the cap; `first` is the number of the first, `parent` the parent's identifier. */
function itemsOf(count, first, parent) {
  return Array.from({ length: Math.min(count, ISSUE_CAP) }, (_, i) => ({
    identifier: `GRA-${String(first + i)}`,
    title: `${WORDS[i % WORDS.length]}${i >= WORDS.length ? ` ${String(Math.floor(i / WORDS.length) + 1)}` : ""}`,
    state: STATES[i % STATES.length],
    parent,
  }));
}

/** The target of a link: an identifier, an issue link or a project link of linear.app. */
function targetOf(value) {
  const text = value.trim();
  const identifier = IDENTIFIER.exec(text.toUpperCase())?.[1];
  if (identifier !== undefined) return { issue: identifier };
  let url;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.hostname !== "linear.app" || url.username !== "") return null;
  const issue = /^\/[^/]+\/issue\/([A-Za-z][A-Za-z0-9]*-\d+)/.exec(url.pathname)?.[1];
  if (issue !== undefined) return { issue: issue.toUpperCase() };
  const project = /^\/[^/]+\/project\/([a-z]+)-[0-9a-f]{12}/.exec(url.pathname)?.[1];
  return project === undefined ? null : { project };
}

/** @returns {{status: number, code: string} | {status: 200, body: object, link: string}} */
export function checkLinearSource(value) {
  const target = targetOf(value);
  if (target === null) return { status: 422, code: "linear_link_invalid" };
  if (target.issue !== undefined) {
    const refused = REFUSED[target.issue];
    if (refused !== undefined) return refused;
    if (target.issue !== "GRA-12") return { status: 409, code: "linear_not_found" };
    const items = itemsOf(6, 101, "GRA-12");
    return {
      status: 200,
      link: "https://linear.app/graphico-app/issue/GRA-12/checkout-redesign",
      body: { kind: "issue", name: "GRA-12 Checkout redesign", issues: items.length, items, capped: false, documents: 0 },
    };
  }
  const project = PROJECTS[target.project];
  if (project === undefined) return { status: 409, code: "linear_not_found" };
  const [name, count, documents] = project;
  const items = itemsOf(count, 1, null);
  return {
    status: 200,
    link: `https://linear.app/graphico-app/project/${target.project}-0a1b2c3d4e5f`,
    body: { kind: "project", name, issues: items.length, items, capped: count > ISSUE_CAP, documents },
  };
}
