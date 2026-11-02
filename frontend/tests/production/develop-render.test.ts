// MCP-M10 (FR-MCP-UI-07, 08; E5): Plan › Develop rendered on the server, in both modes. The copy blocks hold exactly the builders' output
// (a name with quotes stays valid JSON), the guide link is there, and no text of the old mock tab remains.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";

stubCss();
let M: Awaited<ReturnType<typeof load>>;
async function load() {
  return {
    DevelopTab: (await import("../../components/plan/develop/DevelopTab")).DevelopTab,
    S: await import("../../lib/guides/snippets"),
  };
}
before(async () => {
  M = await load();
});

const ID = "0b9c1d4e-1111-4222-8333-444455556666";
const URL = "https://mcp.example.com/mcp";
const html = (el: ReactElement) => renderToStaticMarkup(el);
const unescape = (s: string) =>
  s.replaceAll("&quot;", '"').replaceAll("&#x27;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&amp;", "&");
/** The text of each copy block, in page order. */
function blocks(view: string): string[] {
  const out: string[] = [];
  for (const part of view.split('data-testid="code-block"><code>').slice(1)) out.push(unescape(part.slice(0, part.indexOf("</code>"))));
  return out;
}
/** Tags removed (a linear scan), entities decoded, spaces collapsed. */
function text(view: string): string {
  let out = "";
  let inTag = false;
  for (const ch of view) {
    if (ch === "<") {
      inTag = true;
      out += " ";
    } else if (ch === ">") inTag = false;
    else if (!inTag) out += ch;
  }
  return unescape(out).split(/\s+/).join(" ");
}
const render = (serverName: string, projectName: string | null = "Checkout refactor") =>
  html(createElement(M.DevelopTab, { projectId: ID, info: { projectName, mcpUrl: URL, serverName } }));

test("bind command with the project id, binding JSON, two prompts, in this order", () => {
  const view = render("specflow");
  assert.deepEqual(blocks(view), [
    `/mcp__specflow__select_project ${ID}`,
    M.S.bindingFileJson({ mcpUrl: URL, projectId: ID, projectName: "Checkout refactor" }),
    M.S.DEVELOP_OVERVIEW_PROMPT,
    M.S.DEVELOP_START_PROMPT,
  ]);
  const t = text(view);
  assert.match(t, /This project Use Checkout refactor in your repository/);
  assert.match(t, /Ask your agent Try these in Claude Code/);
  assert.match(t, /Or save the binding file yourself/);
  assert.doesNotMatch(view, /data-mode/);
});

test("the bind command uses the configured server name with the project id", () => {
  assert.equal(blocks(render("acme-specflow"))[0], `/mcp__acme-specflow__select_project ${ID}`);
});

test("the shown binding JSON equals the builder's output for a name with quotes and non-ASCII", () => {
  const name = `The "Q3" plan, Zażółć`;
  const shown = blocks(render("specflow", name))[1];
  assert.equal(shown, M.S.bindingFileJson({ mcpUrl: URL, projectId: ID, projectName: name }));
  assert.deepEqual(JSON.parse(shown), { version: 1, server: URL, project_id: ID, project_name: name });
  assert.match(text(render("specflow", name)), /Use The "Q3" plan, Zażółć in your repository/);
});

test("the closing line links to the MCP guide", () => {
  const view = render("specflow");
  assert.match(text(view), /Claude Code not connected yet\? Set up your coding agent in Guides/);
  assert.match(view, /<a [^>]*href="\/guides\/mcp-server"[^>]*>Set up your coding agent in Guides<\/a>/);
});

test("no mock text remains", () => {
  for (const mode of ["oidc", "dev"] as const) {
    const t = text(render(mode));
    for (const word of [/illustrative/i, /Connect/, /Teach/, /\bRun it\b/, /Workflow/, /Cursor/, /Codex/]) {
      assert.doesNotMatch(t, word);
    }
    assert.doesNotMatch(t, /Tools/);
    assert.doesNotMatch(t, /specflow:select_project/);
  }
});

test("a failed project read keeps the prompts, drops the project card and says so", () => {
  const view = render("specflow", null);
  const t = text(view);
  assert.match(t, /Could not load this project's name/);
  assert.doesNotMatch(t, /in your repository/);
  assert.deepEqual(blocks(view), [M.S.DEVELOP_OVERVIEW_PROMPT, M.S.DEVELOP_START_PROMPT]);
});
