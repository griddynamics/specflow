// MCP-M10 (FR-MCP-UI-03, 05, 06, 08; E5): the MCP guide rendered on the server (the four steps and the team disclosure)
// for each agent, with the configured server name.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";

stubCss();
// Loaded in a `before` hook: this file is CommonJS under tsx, so no top-level await.
let M: Awaited<ReturnType<typeof load>>;
async function load() {
  return {
    McpGuide: (await import("../../components/guides/McpGuide")).McpGuide,
    GuidesHub: (await import("../../components/guides/GuidesHub")).GuidesHub,
    BestPractices: (await import("../../components/guides/BestPractices")).BestPractices,
    Flow101: (await import("../../components/guides/Flow101")).Flow101,
    AppRouterContext: (await import("next/dist/shared/lib/app-router-context.shared-runtime")).AppRouterContext,
    manifest: (await import("../../lib/guides/mcp-tools.json")).default,
  };
}
before(async () => {
  M = await load();
});

const CLAUDE_CODE = "claude-code" as const;
const noop = () => undefined;
const router = { push: noop, replace: noop, back: noop, forward: noop, refresh: noop, prefetch: noop };
const html = (el: ReactElement) => renderToStaticMarkup(createElement(M.AppRouterContext.Provider, { value: router as never }, el));
/** The markup without its tags: every tag becomes one space (a linear scan, no regular expression). */
function stripTags(view: string): string {
  let out = "";
  let inTag = false;
  for (const ch of view) {
    if (ch === "<") {
      inTag = true;
      out += " ";
    } else if (ch === ">") inTag = false;
    else if (!inTag) out += ch;
  }
  return out;
}
const text = (view: string) =>
  stripTags(view)
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .split(/\s+/)
    .join(" ");

const base = { mcpUrl: "https://mcp.example.com/mcp", clientId: "my-client", serverName: "specflow" } as const;

test("Claude Code: no address card, four production steps, team .mcp.json, reference, troubleshooting", () => {
  const view = html(createElement(M.McpGuide, { ...base, agent: CLAUDE_CODE }));
  const t = text(view);
  assert.doesNotMatch(t, /Server address/);
  assert.doesNotMatch(t, /Client ID: my-client/);
  assert.doesNotMatch(t, /Sign-in: your company account/);
  assert.doesNotMatch(t, /Local development/);
  for (const n of [1, 2, 3, 4]) assert.match(t, new RegExp(`Step ${String(n)}`));
  assert.doesNotMatch(t, /Step 5/);
  assert.match(t, /Add SpecFlow to Claude Code/);
  assert.doesNotMatch(t, /\(local\)|add-json/);
  assert.match(
    t,
    /claude mcp add --transport http --scope user --client-id my-client --callback-port 8765 specflow https:\/\/mcp\.example\.com\/mcp/,
  );
  assert.match(t, /For your whole team: commit a \.mcp\.json instead/);
  assert.match(t, /"mcpServers"/);
  assert.match(t, /Authenticate/);
  assert.match(t, /Connect this repository to the SpecFlow project <project name>\./);
  assert.match(t, /Say this to Claude Code, inside your repository/);
  assert.match(t, /Ask Claude Code to connect the repository to your SpecFlow project, and commit \.specflow\/config\.json\./);
  assert.match(t, /Which SpecFlow projects can I work on\?/);
  assert.match(t, /Troubleshooting/);
  assert.doesNotMatch(view, /data-mode/);
  assert.match(view, /aria-pressed|role="radio"|aria-checked/);
});

test("a configured server name shows in the add command, sign-in text, team file and prompts", () => {
  const view = html(createElement(M.McpGuide, { ...base, serverName: "acme-specflow", agent: CLAUDE_CODE }));
  const t = text(view);
  assert.match(t, /--callback-port 8765 acme-specflow https:\/\/mcp\.example\.com\/mcp/);
  assert.match(t, /"acme-specflow": \{/);
  assert.match(t, /choose acme-specflow, then Authenticate/);
  assert.match(t, /claude mcp login acme-specflow --no-browser/);
  assert.match(t, /Type \/mcp, choose acme-specflow, then Authenticate again/);
  assert.match(t, /\/mcp__acme-specflow__select_project/);
  assert.doesNotMatch(t, /login specflow --no-browser|choose specflow,|\/mcp__specflow__/);
});

test("Codex and Cursor show the in-progress state instead of steps", () => {
  for (const [agent, label] of [
    ["codex", "Codex"],
    ["cursor", "Cursor"],
  ] as const) {
    const view = html(createElement(M.McpGuide, { ...base, agent }));
    const t = text(view);
    assert.match(t, new RegExp(`${label} setup is in progress`));
    assert.match(t, /We publish an agent's setup after we have tested it end to end with SpecFlow\. Until then, use Claude Code\./);
    assert.doesNotMatch(t, /Step 1/);
    assert.match(view, /mcp-in-progress/);
    assert.match(t, /What your agent can use/); // the reference stays
  }
});

test("reference card lists every manifest tool and the prompt as a command", () => {
  const prod = text(html(createElement(M.McpGuide, { ...base, agent: CLAUDE_CODE })));
  for (const tool of M.manifest.tools) {
    assert.ok(prod.includes(tool.name) && prod.includes(tool.summary), tool.name);
  }
  assert.match(prod, /Tool, read-only/);
  assert.match(prod, /Command/);
  assert.match(prod, /\/mcp__specflow__select_project/);
  assert.match(prod, /Every call checks that you are a member of the project\./);
});

test("hub: three topic cards with OPEN links and aria-labels", () => {
  const view = html(createElement(M.GuidesHub));
  for (const [title, href] of [
    ["SpecFlow 101", "/guides/specflow-101"],
    ["Best practices", "/guides/best-practices"],
    ["MCP server", "/guides/mcp-server"],
  ]) {
    assert.ok(view.includes(`href="${href}"`), href);
    assert.ok(view.includes(`aria-label="Open ${title}"`), title);
  }
  const t = text(view);
  assert.doesNotMatch(t, /Help/); // no eyebrow
  assert.equal(view.split("<h1").length - 1, 1); // one h1, visually hidden: the breadcrumb is the visible header
  assert.match(view, /<h1 class="shell-sr">Guides<\/h1>/);
  assert.match(t, /New to SpecFlow\? Start with SpecFlow 101/);
  assert.match(text(view), /5 steps/);
  assert.match(text(view), /9 tips/);
  assert.match(text(view), /4 steps for Claude Code/);
});

test("topic header: best practices has the one h1 and no eyebrow repeating the topic", () => {
  const view = html(createElement(M.BestPractices));
  assert.equal(view.split("<h1").length - 1, 1);
  assert.equal(view.split('class="eyebrow guides-muted"').length - 1, 0);
});

test("topic header: the MCP guide has the one h1 and no eyebrow repeating the topic", () => {
  const view = html(createElement(M.McpGuide, { ...base, agent: CLAUDE_CODE }));
  assert.equal(view.split("<h1").length - 1, 1);
  assert.equal(view.split('class="eyebrow guides-muted"').length - 1, 0);
  assert.doesNotMatch(text(view), /MCP server Connect your coding agent/);
});

test("best practices: nine tips in three cards", () => {
  const view = html(createElement(M.BestPractices));
  assert.equal(view.split('class="guides-tip"').length - 1, 9);
});

test("SpecFlow 101: step 1 selected first, Next step and no primary link until step 5", () => {
  const view = html(createElement(M.Flow101, { connectHref: "/guides/mcp-server" }));
  assert.equal(view.split('aria-pressed="true"').length - 1, 1);
  assert.equal(view.split('aria-pressed="false"').length - 1, 4);
  assert.equal(view.split("guides-flow__medal").length - 1, 5); // one medallion per tile
  assert.equal(view.split("guides-flow__link").length - 1, 4); // a connector between tiles
  for (const hidden of ["Add the documents that describe", "SpecFlow maps what the product"]) assert.ok(!view.includes(hidden), hidden);
  assert.match(text(view), /Step 1 of 5/);
  assert.match(text(view), /Next step/);
  assert.doesNotMatch(view, /Connect your coding agent/);
});
