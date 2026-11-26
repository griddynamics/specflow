// plans/LINEAR/LINEAR-PLAN.md, stream S3: the production `LinearPlanPort` over `/projects/{pid}/linear-plan`. Every body is shape-checked: any
// other shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code; a failed access check is an answer.
import { checkLinearPlanAccess, getLinearPlan, listLinearProjects, listLinearTeams, putLinearPlan } from "@/lib/api";
import type {
  LinearPlan,
  LinearPlanAccess,
  LinearPlanCounts,
  LinearPlanField,
  LinearPlanPort,
  LinearPlanSyncStatus,
  LinearProjectChoice,
  LinearTeams,
} from "@/lib/domain/linear-plan";
import { checked, expectShape, guard, isRecord, isString } from "./errors";
import { count, textOrNull, toSync } from "./plan-sync";

const STATUSES: readonly LinearPlanSyncStatus[] = ["idle", "running", "succeeded", "partial", "failed", "skipped"];
const FIELDS: readonly LinearPlanField[] = ["team", "project"];

function toCounts(v: unknown): LinearPlanCounts | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v));
  return {
    created: count(v.created),
    updated: count(v.updated),
    commented: count(v.commented),
    removed: count(v.removed),
    relationsAdded: count(v.relations_added),
    estimateRefused: count(v.estimate_refused),
    unchanged: count(v.unchanged),
    failed: count(v.failed),
  };
}

export function toLinearPlan(v: unknown): LinearPlan {
  expectShape(
    isRecord(v) &&
      typeof v.available === "boolean" &&
      typeof v.enabled === "boolean" &&
      typeof v.created_by_graphico === "boolean" &&
      typeof v.connected_by_me === "boolean",
  );
  return {
    available: v.available,
    enabled: v.enabled,
    teamId: textOrNull(v.team_id),
    teamKey: textOrNull(v.team_key),
    linearProjectId: textOrNull(v.project_id),
    linearProjectName: textOrNull(v.project_name),
    linearProjectUrl: textOrNull(v.project_url),
    createdByGraphico: v.created_by_graphico,
    workspace: textOrNull(v.workspace),
    connectedByMe: v.connected_by_me,
    sync: toSync(v.sync, STATUSES, toCounts),
  };
}

export function toLinearTeams(v: unknown): LinearTeams {
  expectShape(isRecord(v) && isString(v.workspace) && Array.isArray(v.teams));
  return {
    workspace: v.workspace,
    teams: v.teams.map((t: unknown) => {
      expectShape(isRecord(t) && isString(t.id) && isString(t.key) && isString(t.name));
      return { id: t.id, key: t.key, name: t.name };
    }),
  };
}

export function toLinearProjects(v: unknown): LinearProjectChoice[] {
  expectShape(isRecord(v) && Array.isArray(v.projects));
  return v.projects.map((p: unknown) => {
    expectShape(isRecord(p) && isString(p.id) && isString(p.name) && isString(p.url));
    return { id: p.id, name: p.name, url: p.url };
  });
}

export function toLinearPlanAccess(v: unknown): LinearPlanAccess {
  expectShape(isRecord(v));
  if (v.result === "ok") {
    expectShape(isString(v.team_key));
    return { result: "ok", teamKey: v.team_key, projectName: textOrNull(v.project_name) };
  }
  expectShape(v.result === "failed" && isString(v.reason) && v.reason !== "");
  const field = v.field === null || v.field === undefined ? null : FIELDS.find((f) => f === v.field);
  expectShape(field !== undefined);
  return { result: "failed", reason: v.reason, field };
}

export function createHttpLinearPlan(): LinearPlanPort {
  return {
    get: async (projectId) => {
      const body = await guard(() => getLinearPlan(projectId));
      return checked(() => toLinearPlan(body));
    },
    teams: async (projectId) => {
      const body = await guard(() => listLinearTeams(projectId));
      return checked(() => toLinearTeams(body));
    },
    projects: async (projectId, teamId) => {
      const body = await guard(() => listLinearProjects(projectId, teamId));
      return checked(() => toLinearProjects(body));
    },
    save: async (projectId, input) => {
      const body = await guard(() => putLinearPlan(projectId, input.teamId, input.projectId, input.enabled));
      return checked(() => toLinearPlan(body));
    },
    checkAccess: async (projectId, target) => {
      const body = await guard(() => checkLinearPlanAccess(projectId, target.teamId, target.projectId));
      return checked(() => toLinearPlanAccess(body));
    },
  };
}
