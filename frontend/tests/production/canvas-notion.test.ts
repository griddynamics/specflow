// The Notion pages source on the canvas (stream D2, docs/notion/source.md): the checklist of what the bound member shared, the words of every
// closed code of the list and the add, what the API's check says, and the same words reached through the http port against the stub API (tests/support/stub-notion.mjs).
// Port 3342 (stub API). Pure and server-rendered: no browser.
import "../support/css-stub-first.mjs"; // the components under test pull Graphico styles (.css)
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { NotionFormView } from "@/components/v2/canvas/NotionForm";
import { NotionPickerView, type NotionSharedRead } from "@/components/v2/canvas/NotionPicker";
import { NOTION_IMPORT_WORDS, notionAddFailureText, notionCheckText, notionFailureText } from "@/components/v2/canvas/notion-logic";
import { CONNECTORS } from "@/components/v2/connectors";
import { V2_COPY } from "@/components/v2/v2-copy";
import { toNotionCheck, toNotionShared } from "@/lib/data/http/sources";
import { DataError, isDataError } from "@/lib/domain/errors";
import type { NotionShared } from "@/lib/domain/sources";
import { defined } from "../support/defined";
import { html } from "./canvas-form-support";
import { BIG_DATABASE, CHILD_PAGE, DATABASE, emptyForm, NOT_SHARED, notionFormRig, PAGE } from "./notion-form-support";

const rig = notionFormRig(3342);
const failure = (apiCode: string) => notionFailureText(new DataError("validation", false, { apiCode }));

test("the registry has Notion pages in Graph, needing Notion, with no check step", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "notion"));
  assert.deepEqual(
    [spec.zone, spec.app, spec.mark, spec.name, spec.title],
    ["graph", "notion", "notion", "Notion pages", "Add Notion pages"],
  );
  assert.equal(spec.check, null);
});

test("the form first reads what is shared, says what is picked is one source, and Add selected is off until something is ticked", () => {
  const view = emptyForm();
  assert.match(view, /Reading your shared Notion pages…/);
  assert.doesNotMatch(view, /<input|<select/);
  assert.match(view, /What you pick is added as one source/);
  assert.match(view, /disabled=""[^>]*>Add selected<\/button>/);
  assert.doesNotMatch(view, /role="alert"/);
});

test("every closed code a source check or add can answer has its own words; an unknown code is the general failure", () => {
  const codes = [
    "notion_not_connected",
    "notion_reconnect_required",
    "notion_unavailable",
    "notion_unreachable",
    "notion_auth_failed",
    "notion_rate_limited",
    "notion_link_invalid",
    "notion_not_shared",
    "source_not_found",
  ];
  const words = codes.map(failure);
  for (const w of words) assert.notEqual(w, V2_COPY.actionFailed);
  assert.equal(new Set(words).size, codes.length, "each code says something of its own");
  assert.match(failure("notion_not_connected"), /Connect Notion in Integrations first/);
  assert.match(failure("notion_reconnect_required"), /needs a reconnect in Integrations/);
  assert.match(failure("notion_link_invalid"), /not a link to a Notion page or database/);
  assert.match(failure("notion_not_shared"), /no longer shared with Graphico/);
  assert.match(failure("notion_rate_limited"), /busy right now/);
  assert.match(failure("notion_unavailable"), /not available on this server/);
  assert.match(failure("notion_unreachable"), /did not answer/);
  assert.match(failure("notion_auth_failed"), /Ask your admin to check the Notion setup/);
  assert.equal(failure("something_new"), V2_COPY.actionFailed);
  assert.equal(failure("notion_page_gone"), V2_COPY.actionFailed);
  assert.equal(notionFailureText(new Error("x")), V2_COPY.actionFailed);
  assert.equal(notionFailureText(null), V2_COPY.actionFailed);
});

test("the words never show a code, and a prototype key is no code", () => {
  assert.equal(failure("constructor"), V2_COPY.actionFailed);
  assert.equal(failure("__proto__"), V2_COPY.actionFailed);
  for (const text of [...Object.values(NOTION_IMPORT_WORDS), failure("notion_auth_failed"), failure("notion_unreachable")]) {
    assert.doesNotMatch(text, /notion_[a-z_]+/);
  }
});

test("a passed check says what an import takes: a page with its sub-pages, a database with its rows, the cap", () => {
  const page = { kind: "page", title: "Engineering Wiki", pages: 4, capped: false } as const;
  assert.equal(notionCheckText(page), "Page Engineering Wiki · this page and 3 sub-pages.");
  assert.equal(notionCheckText({ ...page, pages: 2 }), "Page Engineering Wiki · this page and 1 sub-page.");
  assert.equal(notionCheckText({ ...page, pages: 1 }), "Page Engineering Wiki · this page, no sub-pages.");
  assert.equal(
    notionCheckText({ ...page, pages: 100, capped: true }),
    "Page Engineering Wiki · this page and the first 99 sub-pages (more were found).",
  );
  const db = { kind: "database", title: "Requirements", pages: 42, capped: false } as const;
  assert.equal(notionCheckText(db), "Database Requirements · 42 rows.");
  assert.equal(notionCheckText({ ...db, pages: 1 }), "Database Requirements · 1 row.");
  assert.equal(notionCheckText({ ...db, pages: 0 }), "Database Requirements · 0 rows.");
  assert.equal(notionCheckText({ ...db, pages: 100, capped: true }), "Database Requirements · the first 100 rows (more were found).");
});

test("against the stub: a page, a database and a capped database are checked in words", async () => {
  const pid = await rig.project();
  assert.equal(await rig.checkSays(pid, PAGE), "Page Engineering Wiki · this page and 3 sub-pages.");
  assert.equal(await rig.checkSays(pid, DATABASE), "Database Requirements · 42 rows.");
  assert.equal(await rig.checkSays(pid, BIG_DATABASE), "Database Big database · the first 100 rows (more were found).");
});

test("against the stub: a bad link and a page nobody shared are said in words by the check and the add", async () => {
  const pid = await rig.project();
  assert.equal(await rig.checkSays(pid, "https://example.com/x-423ed33606458137b924dd97aa16e0a9"), failure("notion_link_invalid"));
  assert.equal(await rig.checkSays(pid, "not a link"), failure("notion_link_invalid"));
  assert.equal(await rig.checkSays(pid, NOT_SHARED), failure("notion_not_shared"));
  assert.match(await rig.addSays(pid, PAGE, NOT_SHARED), /^Nothing was added: one of the picked pages is no longer shared/);
});

test("against the stub: a project without a Notion binding, or with an expired one, is refused by the check and by the add", async () => {
  const unbound = await rig.project("none");
  assert.equal(await rig.checkSays(unbound, PAGE), failure("notion_not_connected"));
  assert.equal(await rig.addSays(unbound, PAGE), failure("notion_not_connected"));
  const expired = await rig.project("reconnect_required");
  assert.equal(await rig.checkSays(expired, PAGE), failure("notion_reconnect_required"));
  assert.equal(await rig.addSays(expired, PAGE), failure("notion_reconnect_required"));
});

test("against the stub: the picked links are posted once, as one source named after the first", async () => {
  const pid = await rig.project();
  rig.ctx.sent.length = 0;
  assert.equal(await rig.addSays(pid, PAGE, CHILD_PAGE, DATABASE), "added");
  assert.deepEqual(
    rig.ctx.sent.map((s) => [s.method, s.path, s.body]),
    [["POST", `/api/backend/projects/${pid}/sources/notion`, JSON.stringify({ links: [PAGE, CHILD_PAGE, DATABASE] })]],
  );
});

test("the check wire keeps kind, title, count and cap; a fractional count, a missing title or an extra kind is invalid_response", () => {
  assert.deepEqual(toNotionCheck({ kind: "database", title: "T", pages: 0, capped: false, extra: 1 }), {
    kind: "database",
    title: "T",
    pages: 0,
    capped: false,
  });
  for (const bad of [
    { kind: "page", pages: 1, capped: false },
    { kind: "page", title: "T", pages: 1.5, capped: false },
    { kind: "space", title: "T", pages: 1, capped: false },
    [],
  ]) {
    assert.throws(
      () => toNotionCheck(bad),
      (e) => isDataError(e) && e.code === "invalid_response",
    );
  }
});

test("a link over the length cap is a 422 validation_failed and says the link is not valid", () => {
  const long = notionFailureText(new DataError("validation", false, { apiCode: "validation_failed" }));
  assert.equal(long, failure("notion_link_invalid"));
});

const WIKI = "Engineering Wiki";
const SHARED: NotionShared = {
  items: [
    { kind: "page", title: WIKI, link: PAGE, parentTitle: null },
    { kind: "page", title: "Onboarding", link: CHILD_PAGE, parentTitle: WIKI },
    { kind: "database", title: "Requirements", link: DATABASE, parentTitle: null },
  ],
  capped: false,
};
const ready = (shared: NotionShared = SHARED): NotionSharedRead => ({ kind: "ready", shared });

const view = (over: Partial<Parameters<typeof NotionFormView>[0]>) =>
  html(
    createElement(NotionFormView, {
      shared: ready(),
      picked: new Set<string>(),
      busy: false,
      problem: null,
      onPick: () => undefined,
      onBack: () => undefined,
      onAdd: () => undefined,
      ...over,
    }),
  );

test("the checklist lists the shared pages first, with their parent page, then the databases, nothing ticked", () => {
  const list = view({});
  assert.match(list, /<legend[^>]*>Shared with Graphico<\/legend>/);
  assert.equal(list.match(/type="checkbox"/g)?.length, 3);
  assert.doesNotMatch(list, /checked=""/);
  assert.match(list, /Engineering Wiki[\s\S]*Onboarding[\s\S]*in Engineering Wiki[\s\S]*Requirements[\s\S]*Database/);
  assert.match(list, />0 picked</);
  assert.match(list, /disabled=""[^>]*>Add selected<\/button>/);
});

test("ticked items are counted and enable Add selected; while the adds run the list and the button are off", () => {
  const two = view({ picked: new Set([PAGE, DATABASE]) });
  assert.equal(two.match(/checked=""/g)?.length, 2);
  assert.match(two, />2 picked</);
  assert.doesNotMatch(two, /disabled=""[^>]*>Add selected/);
  const busy = view({ picked: new Set([PAGE]), busy: true });
  assert.equal(busy.match(/type="checkbox"[^>]*disabled=""|disabled=""[^>]*type="checkbox"/g)?.length, 3);
  assert.match(busy, /aria-busy="true"[^>]*>Adding…<\/button>/);
});

test("Add selected hands over the picked links in the list's order, without one that left the list; a failed add is said in words", () => {
  let sent: readonly string[] = [];
  const form = NotionFormView({
    shared: ready(),
    picked: new Set([DATABASE, PAGE, NOT_SHARED]),
    busy: false,
    problem: null,
    onPick: () => undefined,
    onBack: () => undefined,
    onAdd: (links) => {
      sent = links;
    },
  });
  const button = (form.props as { action: { props: { onClick: () => void } } }).action;
  button.props.onClick();
  assert.deepEqual(sent, [PAGE, DATABASE]);
  const failed = view({
    picked: new Set([DATABASE]),
    problem: notionAddFailureText(new DataError("conflict", false, { apiCode: "notion_not_connected" })),
  });
  assert.match(failed, /role="alert"[^>]*>Connect Notion in Integrations first\./);
  assert.match(
    notionAddFailureText(new DataError("conflict", false, { apiCode: "notion_not_shared" })),
    /^Nothing was added: one of the picked pages is no longer shared with Graphico\. The list is read again/,
  );
});

test("nothing shared, a refused read and a capped list are said in words", () => {
  const empty = view({ shared: ready({ items: [], capped: false }) });
  assert.doesNotMatch(empty, /type="checkbox"/);
  assert.match(empty, /Nothing is shared with Graphico yet/);
  const failed = view({ shared: { kind: "failed", text: failure("notion_not_connected") } });
  assert.match(failed, /role="alert"[^>]*>[^<]*Connect Notion in Integrations first/);
  assert.match(view({ shared: ready({ ...SHARED, capped: true }) }), /Only the first 300 pages and databases are listed/);
});

test("the plan's parent page picker offers the shared pages only, one to pick", () => {
  const picker = html(
    createElement(NotionPickerView, { read: ready(), label: "Parent page", value: PAGE, disabled: false, onChange: () => undefined }),
  );
  assert.doesNotMatch(picker, /Requirements/);
  assert.match(picker, />Choose…<\/option>/);
  assert.match(picker, /<option value="[^"]*e0a9" selected="">Engineering Wiki<\/option>/);
  assert.match(picker, />Onboarding · in Engineering Wiki<\/option>/);
});

test("the shared wire keeps kind, title, link and parent; anything else is invalid_response", () => {
  assert.deepEqual(toNotionShared({ items: [{ kind: "page", title: "T", link: PAGE, parent_title: null, extra: 1 }], capped: true }), {
    items: [{ kind: "page", title: "T", link: PAGE, parentTitle: null }],
    capped: true,
  });
  for (const bad of [
    { items: [], capped: "no" },
    { items: [{ kind: "row", title: "T", link: PAGE, parent_title: null }], capped: false },
    { items: [{ kind: "page", title: "T", link: PAGE }], capped: false },
    {},
  ]) {
    assert.throws(
      () => toNotionShared(bad),
      (e) => isDataError(e) && e.code === "invalid_response",
    );
  }
});

test("against the stub: the picker reads what the bound member shared; an unbound or expired project is said in words", async () => {
  const pid = await rig.project();
  const shared = await rig.sources.listNotion(pid);
  assert.deepEqual(
    shared.items.map((i) => [i.kind, i.title, i.parentTitle]),
    [
      ["database", "Big database", null],
      ["page", WIKI, null],
      ["page", "Old notes", null],
      ["page", "Onboarding", WIKI],
      ["page", "Plans", null],
      ["database", "Requirements", null],
    ],
  );
  assert.equal(await rig.checkSays(pid, defined(shared.items[1]).link), "Page Engineering Wiki · this page and 3 sub-pages.");
  assert.equal(await rig.checkSays(pid, defined(shared.items[2]).link), failure("notion_not_shared"));
  const unbound = await rig.project("none");
  assert.equal(await rig.sources.listNotion(unbound).then(() => "listed", notionFailureText), failure("notion_not_connected"));
  const expired = await rig.project("reconnect_required");
  assert.equal(await rig.sources.listNotion(expired).then(() => "listed", notionFailureText), failure("notion_reconnect_required"));
});
