// Not a test: what the Notion plan tests share (E2). `canvas-fixtures.ts` stays frozen. The port is the real wire reading (`toNotionPlan`,
// `toNotionPlanAccess`) over the in-process `createNotionPlanModel` of the stub: no server, no port number; a refusal of the model is the
// DataError the http adapter makes of it (`detail.apiCode`).
import { buildCanvasModel } from "@/components/v2/canvas/canvas-model";
import type { CanvasInput, NodeView } from "@/components/v2/canvas/canvas-types";
import { NODE_MAPPERS } from "@/components/v2/connectors";
import { toNotionPlan, toNotionPlanAccess } from "@/lib/data/http/notion-plan";
import { DataError } from "@/lib/domain/errors";
import type { NotionPlan, NotionPlanCounts, NotionPlanPort, NotionPlanSync } from "@/lib/domain/notion-plan";
import { defined } from "../support/defined";
import { createNotionPlanModel } from "../support/stub-notion.mjs";
import { BUILT, GAPS, READY_PLAN } from "./canvas-fixtures";

export const COUNTS: NotionPlanCounts = { created: 0, updated: 0, commented: 0, removed: 0, unchanged: 0, failed: 0, deferred: 0 };

export const SYNC: NotionPlanSync = {
  status: "succeeded",
  queued: false,
  finishedAt: "2026-11-25T09:00:05Z",
  errorCode: null,
  counts: { ...COUNTS, created: 6 },
};

/** The destination the API reports for a project that added Notion: on, 6 rows made, acting as someone else. */
export const TARGET: NotionPlan = {
  available: true,
  parentPageId: "423ed33606458163a8dde12275fc660d",
  parentPageTitle: "Plans",
  parentPageUrl: "https://www.notion.so/Plans-423ed33606458163a8dde12275fc660d",
  databaseUrl: null,
  enabled: true,
  connectedByName: "Sample Workspace",
  connectedByMe: false,
  sync: SYNC,
};

export const sync = (over: Partial<NotionPlanSync>): { sync: NotionPlanSync } => ({ sync: { ...SYNC, ...over } });

/** The Plan zone's Notion node of a built canvas whose plan is ready, or undefined when there is none. */
export const maybeNodeOf = (over: Partial<NotionPlan>, input: Partial<CanvasInput> = {}): NodeView | undefined =>
  buildCanvasModel(
    BUILT({ gaps: GAPS, plan: READY_PLAN, notionPlan: { ...TARGET, ...over }, ...input }),
    NODE_MAPPERS,
  ).zones.plan.nodes.find((n) => n.kind === "notion_plan");
export const nodeOf = (over: Partial<NotionPlan>, input: Partial<CanvasInput> = {}): NodeView => defined(maybeNodeOf(over, input));

export const PARENT = "https://www.notion.so/Plans-423ed33606458163a8dde12275fc660d";
export const DATABASE_PARENT = "https://www.notion.so/Requirements-423ed3360645819b8a30d73a847000db";
export const NOT_SHARED = "https://www.notion.so/Secret-423ed33606458163a8dde12275fc0404";

export type Health = "active" | "reconnect_required" | "none";

/** A port over the stub model of one project whose Notion binding has the given health. */
export function modelPort(pid: string, health: Health = "active") {
  const model = createNotionPlanModel(() => health);
  const refuse = <T>(run: () => T): T => {
    try {
      return run();
    } catch (e) {
      if (e instanceof Error && "status" in e && "code" in e) throw new DataError("conflict", false, { apiCode: String(e.code) });
      throw e;
    }
  };
  const port: NotionPlanPort = {
    get: async () => toNotionPlan(model.get(pid)),
    save: async (_pid, input) => toNotionPlan(refuse(() => model.put(pid, { link: input.link, enabled: input.enabled }))),
    checkAccess: async (_pid, link) => toNotionPlanAccess(refuse(() => model.check(pid, { link }))),
  };
  return { model, port };
}
