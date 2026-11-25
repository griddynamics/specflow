// Node mapper of the Notion plan destination: one per project, under Plan (docs/notion/plan.md "In the canvas"). Stream E2 fills it, as
// `nodes-github-plan.ts` does for GitHub Projects: it waits for the plan, syncs while a pass runs (the wire moves only then) and stops only for what
// the API reports (`input.notionPlan`, `stoppedIntegration(input, "notion")`). W0 stub: no node yet.
import type { NodeMapper } from "./canvas-types";

export const notionPlanNodes: NodeMapper = () => [];
