// MCP-M10 U5: the Plan | Develop switch is one shared component (idle and ready stages). It renders both segments, marks the selected one,
// and builds its URL with the plan route convention. The idle and ready screens that mount it are covered in e2e-production.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";

stubCss();
let M: Awaited<ReturnType<typeof load>>;
async function load() {
  return {
    PlanViewSwitch: (await import("../../components/plan/PlanViewSwitch")).PlanViewSwitch,
    DataProvider: (await import("../../lib/data/provider")).DataProvider,
  };
}
before(async () => {
  M = await load();
});

const ID = "0b9c1d4e-1111-4222-8333-444455556666";
function render(view: "plan" | "develop"): string {
  return renderToStaticMarkup(createElement(M.DataProvider, null, createElement(M.PlanViewSwitch, { projectId: ID, view })));
}

test("the switch shows Plan and Develop and marks the selected view", () => {
  for (const choice of ["plan", "develop"] as const) {
    const view = render(choice);
    assert.match(view, /Plan view/);
    assert.match(view, />Plan</);
    assert.match(view, />Develop</);
    const selected = [...view.matchAll(/<button[^>]*aria-(?:checked|pressed|selected)="true"[^>]*>([^<]*)</g)].map((m) => m[1]);
    assert.deepEqual(selected, [choice === "plan" ? "Plan" : "Develop"]);
  }
});
