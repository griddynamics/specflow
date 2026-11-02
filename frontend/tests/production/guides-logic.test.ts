// MCP-M10 (FR-MCP-UI-01, 05): the Guides routes, the sidebar rules, the `?agent=` parsing and the topic lookup.
import assert from "node:assert/strict";
import { test } from "node:test";
import { SECTIONS, SIDEBAR_SECTIONS, sectionHref, sectionOf } from "../../components/shell/shell-logic";
import { AGENTS, DEFAULT_AGENT, agentLabel, isTested, parseAgent } from "../../lib/guides/agent";
import { FLOW_STEPS, TOPIC_CARDS, topicTitle } from "../../lib/guides/content";
import { GUIDE_TOPICS, ROUTES, parseGuideTopic, routesFor } from "../../lib/routes";

const routes = routesFor("production");

test("route builder: /guides and /guides/{topic}", () => {
  assert.equal(ROUTES.guides.template, "/guides");
  assert.equal(routes.guides(), "/guides");
  assert.equal(routes.guides("specflow-101"), "/guides/specflow-101");
  assert.equal(routes.guides("best-practices"), "/guides/best-practices");
  assert.equal(routes.guides("mcp-server"), "/guides/mcp-server");
  assert.deepEqual([...GUIDE_TOPICS], ["specflow-101", "best-practices", "mcp-server"]);
});

test("topic lookup: an unknown topic is not found", () => {
  for (const t of GUIDE_TOPICS) assert.equal(parseGuideTopic(t), t);
  for (const t of ["nope", "", "MCP-SERVER", "mcp-server/", "../connections", "constructor", "__proto__"]) {
    assert.equal(parseGuideTopic(t), null, t);
  }
});

test("sidebar: Guides is the last section, needs no project and stays active on every topic", () => {
  assert.equal(SECTIONS.at(-1)?.value, "guides");
  assert.equal(SECTIONS.at(-1)?.icon, "question");
  assert.equal(SIDEBAR_SECTIONS.at(-1)?.value, "guides");
  assert.equal(sectionHref(routes, "guides", null), "/guides");
  assert.equal(sectionHref(routes, "guides", "p1"), "/guides");
  for (const p of ["/guides", "/guides/", "/guides/mcp-server", "/guides/specflow-101", "/guides/best-practices"]) {
    assert.equal(sectionOf(p), "guides", p);
  }
  assert.equal(sectionOf("/guidesX"), null);
  assert.equal(sectionOf("/connections"), "connections");
});

test("hub cards: one per topic, in order, each with a title", () => {
  assert.deepEqual(
    TOPIC_CARDS.map((c) => c.topic),
    [...GUIDE_TOPICS],
  );
  assert.equal(topicTitle("mcp-server"), "MCP server");
  assert.equal(FLOW_STEPS.length, 5);
});

test("agent picker: only Claude Code is tested; ?agent= parsing falls back to Claude Code", () => {
  assert.deepEqual(
    AGENTS.map((a) => [a.value, isTested(a.value)]),
    [
      ["claude-code", true],
      ["codex", false],
      ["cursor", false],
    ],
  );
  assert.equal(parseAgent("codex"), "codex");
  assert.equal(parseAgent("cursor"), "cursor");
  assert.equal(parseAgent("claude-code"), "claude-code");
  for (const v of [undefined, "", "gemini", "CODEX", ["bogus", "codex"]]) assert.equal(parseAgent(v), DEFAULT_AGENT);
  assert.equal(parseAgent(["cursor", "codex"]), "cursor");
  assert.equal(agentLabel("codex"), "Codex");
});
