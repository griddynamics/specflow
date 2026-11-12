// MCP-TOOLING session 10: the finding row rendered on the server. A blocking finding says so in words, a closed one shows who answered and the
// note, references are links only for http(s) URLs (new tab, noopener), the spec nodes are buttons, and an answered finding has no actions.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";
import type { Finding } from "@/lib/domain/work";

stubCss();
let M: Awaited<ReturnType<typeof load>>;
async function load() {
  return { FindingRow: (await import("../../components/plan/work/FindingRow")).FindingRow };
}
before(async () => {
  M = await load();
});

const finding = (over: Partial<Finding> = {}): Finding => ({
  findingId: "FND::1",
  kind: "question",
  text: "Which currencies does the API accept?",
  status: "open",
  blocking: true,
  about: ["FEAT::API", "FEAT::UI"],
  refersTo: [
    { type: "url", value: "https://example.com/spec" },
    { type: "commit", value: "e44f0c1" },
    { type: "url", value: "javascript:alert(1)" },
  ],
  storyId: "STORY::A::STUCK",
  storyTitle: "Checkout",
  author: { name: "Alice", you: true, agentLabel: null },
  createdAt: "2026-01-01T10:00:00Z",
  resolvedBy: null,
  resolvedAt: null,
  resolutionNote: null,
  ...over,
});
const render = (f: Finding) =>
  renderToStaticMarkup(createElement(M.FindingRow, { finding: f, onAction: () => undefined, onNode: () => undefined }));

test("an open blocking finding shows Blocking, the kind, the story it blocks, node buttons, safe links and both actions", () => {
  const view = render(finding());
  assert.match(view, /data-blocking/);
  assert.match(view, />Question</);
  assert.match(view, /Blocking · Checkout</);
  assert.equal((view.match(/data-testid="finding-node"/g) ?? []).length, 2);
  assert.equal((view.match(/data-testid="finding-ref"/g) ?? []).length, 1);
  assert.match(view, /href="https:\/\/example.com\/spec" target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(view, /href="javascript:/);
  assert.match(view, /<code[^>]*>javascript:alert\(1\)<\/code>/);
  assert.match(view, /Alice \(you\)/);
  assert.match(view, /data-testid="finding-resolve"/);
  assert.match(view, /data-testid="finding-dismiss"/);
});

test("a plain finding has no Blocking tag; the finding text is escaped", () => {
  const view = render(finding({ blocking: false, kind: "note", storyId: null, storyTitle: null, text: "<b>bold</b> & more" }));
  assert.doesNotMatch(view, />Blocking</);
  assert.doesNotMatch(view, /Checkout/);
  assert.match(view, /&lt;b&gt;bold&lt;\/b&gt; &amp; more/);
});

test("a reference with credentials in its URL is text, not a link; the label carries a separator", async () => {
  const { refHref } = await import("../../components/plan/work/work-logic");
  assert.equal(refHref({ type: "url", value: "https://user:secret@example.com/x" }), null);
  assert.equal(refHref({ type: "pr", value: "https://token@github.com/o/r/pull/1" }), null);
  assert.equal(refHref({ type: "url", value: "https://example.com/x" }), "https://example.com/x");
  const view = render(finding({ refersTo: [{ type: "url", value: "https://u:p@example.com/" }] }));
  assert.doesNotMatch(view, /data-testid="finding-ref"/);
  assert.match(view, />Link:</);
});

test("a resolved finding shows who answered, when and the note, and has no actions", () => {
  const view = render(
    finding({
      status: "resolved",
      resolvedBy: { name: "Ben", you: false, agentLabel: null },
      resolvedAt: "2026-01-02T10:00:00Z",
      resolutionNote: "Euro and dollar only.",
    }),
  );
  assert.match(view, />Resolved</);
  assert.match(view, /Resolved by Ben/);
  assert.match(view, /Euro and dollar only\./);
  assert.doesNotMatch(view, /finding-resolve|finding-dismiss/);
});
