// MCP-M10 U5: the Plan | Develop switch is one shared component (idle and ready stages). It renders both segments, marks the selected one,
// and builds its URL with the plan route convention. The idle and ready screens that mount it are covered in e2e-production.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";

stubCss();
let M: {
  PlanViewSwitch: typeof import("../../components/plan/PlanViewSwitch").PlanViewSwitch;
  DataContext: typeof import("../../lib/data/provider").DataContext;
  routesFor: typeof import("../../lib/routes").routesFor;
};
before(async () => {
  M = {
    PlanViewSwitch: (await import("../../components/plan/PlanViewSwitch")).PlanViewSwitch,
    DataContext: (await import("../../lib/data/provider")).DataContext,
    routesFor: (await import("../../lib/routes")).routesFor,
  };
});

const ID = "0b9c1d4e-1111-4222-8333-444455556666";
function render(view: "plan" | "develop"): string {
  const value = { routes: M.routesFor("production") } as unknown as NonNullable<React.ContextType<typeof M.DataContext>>;
  return renderToStaticMarkup(createElement(M.DataContext.Provider, { value }, createElement(M.PlanViewSwitch, { projectId: ID, view })));
}

test("the switch shows Plan and Develop and marks the selected view", () => {
  for (const view of ["plan", "develop"] as const) {
    const html = render(view);
    assert.match(html, /Plan view/);
    assert.match(html, />Plan</);
    assert.match(html, />Develop</);
    const selected = [...html.matchAll(/<button[^>]*aria-(?:checked|pressed|selected)="true"[^>]*>([^<]*)</g)].map((m) => m[1]);
    assert.deepEqual(selected, [view === "plan" ? "Plan" : "Develop"]);
  }
});
