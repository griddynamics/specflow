// The Notion link source on the canvas (stream D2, docs/notion/source.md): the form before any answer, what each answer says, the words of every
// closed code of the check and the add, and the same words reached through the http port against the stub API (tests/support/stub-notion.mjs).
// Port 3342 (stub API). Pure and server-rendered: no browser.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { NotionAnswer, NotionFormView } from "@/components/v2/canvas/NotionForm";
import { NOTION_WORDS, notionCheckText, notionFailureText } from "@/components/v2/canvas/notion-logic";
import { CONNECTORS } from "@/components/v2/connectors";
import { V2_COPY } from "@/components/v2/v2-copy";
import { toNotionCheck } from "@/lib/data/http/sources";
import { DataError, isDataError } from "@/lib/domain/errors";
import { defined } from "../support/defined";
import { html } from "./canvas-form-support";
import { BIG_DATABASE, DATABASE, emptyForm, NOT_SHARED, notionFormRig, PAGE } from "./notion-form-support";

const rig = notionFormRig(3342);
const failure = (apiCode: string) => notionFailureText(new DataError("validation", false, { apiCode }));

test("the registry has the Notion link in Graph, needing Notion, with the check labels", () => {
  const spec = defined(CONNECTORS.find((c) => c.kind === "notion"));
  assert.deepEqual([spec.zone, spec.app, spec.mark], ["graph", "notion", "notion"]);
  assert.deepEqual(spec.check, { test: "Check link", testing: "Checking…", add: "Add source" });
});

test("the form asks for the link, says it reads once, and Check link is off until there is a link", () => {
  const view = emptyForm();
  assert.match(view, />Page or database link<\/label>/);
  assert.match(view, /read once when it is added/);
  assert.match(view, /disabled=""[^>]*>Check link<\/button>/);
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
  assert.match(failure("notion_not_shared"), /Share it with Graphico when you connect Notion/);
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
  for (const text of [...Object.values(NOTION_WORDS), failure("notion_auth_failed"), failure("notion_unreachable")]) {
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

test("the answer region shows a passed check as a status and a refusal as an alert", () => {
  const none = html(createElement(NotionAnswer, { result: null, problem: null }));
  assert.match(none, /<div role="status"><\/div>/);
  assert.doesNotMatch(none, /role="alert"/);
  const checked = html(
    createElement(NotionAnswer, { result: { kind: "database", title: "Requirements", pages: 42, capped: false }, problem: null }),
  );
  assert.match(checked, /role="status"[^>]*>.*Database Requirements · 42 rows\./);
  assert.doesNotMatch(checked, /role="alert"/);
  const refused = html(createElement(NotionAnswer, { result: null, problem: failure("notion_not_connected") }));
  assert.match(refused, /role="alert"[^>]*>.*Connect Notion in Integrations first/);
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
  assert.equal(await rig.addSays(pid, NOT_SHARED), failure("notion_not_shared"));
});

test("against the stub: a project without a Notion binding, or with an expired one, is refused by the check and by the add", async () => {
  const unbound = await rig.project("none");
  assert.equal(await rig.checkSays(unbound, PAGE), failure("notion_not_connected"));
  assert.equal(await rig.addSays(unbound, PAGE), failure("notion_not_connected"));
  const expired = await rig.project("reconnect_required");
  assert.equal(await rig.checkSays(expired, PAGE), failure("notion_reconnect_required"));
  assert.equal(await rig.addSays(expired, PAGE), failure("notion_reconnect_required"));
});

test("against the stub: an add posts the link and nothing else", async () => {
  const pid = await rig.project();
  rig.ctx.sent.length = 0;
  assert.equal(await rig.addSays(pid, PAGE), "added");
  assert.deepEqual(
    rig.ctx.sent.map((s) => [s.method, s.path, s.body]),
    [["POST", `/api/backend/projects/${pid}/sources/notion`, JSON.stringify({ link: PAGE })]],
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

test("a link over the length cap is a 422 validation_failed and says the link is not valid; the field stops at 2048", () => {
  const long = notionFailureText(new DataError("validation", false, { apiCode: "validation_failed" }));
  assert.equal(long, failure("notion_link_invalid"));
  assert.match(emptyForm(), /maxLength="2048"/);
});

const view = (over: Partial<Parameters<typeof NotionFormView>[0]>) =>
  html(
    createElement(NotionFormView, {
      link: PAGE,
      onLink: () => undefined,
      busy: false,
      label: "Check link",
      result: null,
      problem: null,
      onBack: () => undefined,
      onSubmit: () => undefined,
      ...over,
    }),
  );

test("while a call runs the field is read-only and the button is busy and says Checking…", () => {
  const busy = view({ busy: true, label: "Checking…" });
  assert.match(busy, /readOnly=""/);
  assert.match(busy, /aria-busy="true"[^>]*>Checking…<\/button>/);
  assert.match(busy, /disabled=""/);
  assert.doesNotMatch(view({}), /readOnly=""/);
});

test("after a passed check the line says what is imported and the button says Add source", () => {
  const checked = view({ label: "Add source", result: { kind: "page", title: "Engineering Wiki", pages: 4, capped: false } });
  assert.match(checked, /Page Engineering Wiki · this page and 3 sub-pages\./);
  assert.match(checked, />Add source<\/button>/);
  assert.doesNotMatch(checked, /disabled=""/);
});
