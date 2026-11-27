// docs/projects/ui.md#route-and-sidebar: the `settings` route key, its builder and query parser, and the sidebar entry
// (SECTIONS, sectionHref, sectionOf) with its nav-spec twin. Pure: no server, no React.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ROUTES, parseSettingsQuery, routesFor } from "@/lib/routes";
import { SECTIONS, SIDEBAR_SECTIONS, sectionHref, sectionOf } from "@/components/shared/shell/shell-logic";

const routes = routesFor("production");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

test("route key settings: template, builder with and without a tab", () => {
  assert.equal(ROUTES.settings.template, "/projects/{projectId}/settings");
  assert.deepEqual(ROUTES.settings.trees, ["production"]);
  assert.equal(routes.settings("p1"), "/projects/p1/settings");
  assert.equal(routes.settings("p1", { tab: "general" }), "/projects/p1/settings?tab=general");
  assert.equal(routes.settings("p1", { tab: "jira" }), "/projects/p1/settings?tab=jira");
  assert.equal(routes.settings("a b/c"), "/projects/a%20b%2Fc/settings", "the project id is encoded");
});

test("parseSettingsQuery: a missing or unknown tab is general, never an error", () => {
  assert.deepEqual(parseSettingsQuery(new URLSearchParams("")), { tab: "general" });
  assert.deepEqual(parseSettingsQuery(new URLSearchParams("tab=general")), { tab: "general" });
  assert.deepEqual(parseSettingsQuery(new URLSearchParams("tab=jira")), { tab: "jira" });
  for (const t of ["nope", "", "GENERAL", "JIRA", "constructor", "__proto__"])
    assert.deepEqual(parseSettingsQuery(new URLSearchParams({ tab: t })), { tab: "general" }, t);
});

test("sidebar: Settings (gear-six) sits after SpecBot and before the Help group; its href is the route; it needs a project", () => {
  const keys = SIDEBAR_SECTIONS.map((s) => s.value);
  assert.deepEqual(keys, ["missionControl", "gaps", "plan", "specbot", "settings", "guides"]);
  const settings = SECTIONS.find((s) => s.value === "settings");
  assert.deepEqual(settings, { value: "settings", label: "Settings", icon: "gear-six" });
  assert.equal(sectionHref(routes, "settings", "p1"), "/projects/p1/settings");
  assert.equal(sectionHref(routes, "settings", null), "/", "without a project it leads to the entry route");
  assert.equal(sectionOf("/projects/p1/settings"), "settings");
  assert.equal(sectionOf("/projects/p1/settings/"), "settings");
  assert.equal(sectionOf("/projects/p1/settingsX"), null);
});

test("nav-spec.json lists the sections in the order of SECTIONS", () => {
  const spec = JSON.parse(readFileSync(path.join(root, "validation/nav-spec.json"), "utf8")) as {
    items: { label: string; key: string; icon: string }[];
  };
  assert.deepEqual(
    spec.items,
    SECTIONS.map((s) => ({ label: s.label, key: s.value, icon: s.icon })),
  );
});
