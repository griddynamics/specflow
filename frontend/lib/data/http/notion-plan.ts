// docs/notion/plan.md: the production `NotionPlanPort` over `/projects/{pid}/notion-plan`. Every body is shape-checked: any other shape is
// `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code; a failed access check is an answer.
import { checkNotionPlanAccess, getNotionPlan, putNotionPlan } from "@/lib/api";
import type {
  NotionPlan,
  NotionPlanAccess,
  NotionPlanCounts,
  NotionPlanPort,
  NotionPlanSync,
  NotionSyncStatus,
} from "@/lib/domain/notion-plan";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const STATUSES: readonly NotionSyncStatus[] = ["idle", "running", "succeeded", "partial", "failed", "skipped"];

const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || v === undefined || isString(v));
  return v ?? null;
};

const count = (v: unknown): number => {
  expectShape(typeof v === "number" && Number.isSafeInteger(v) && v >= 0);
  return v;
};

function toCounts(v: unknown): NotionPlanCounts | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v));
  return {
    created: count(v.created),
    updated: count(v.updated),
    commented: count(v.commented),
    removed: count(v.removed),
    unchanged: count(v.unchanged),
    failed: count(v.failed),
    deferred: count(v.deferred),
  };
}

function toSync(v: unknown): NotionPlanSync | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v) && typeof v.queued === "boolean");
  const finishedAt = textOrNull(v.finished_at);
  expectShape(finishedAt === null || !Number.isNaN(Date.parse(finishedAt)));
  const status = STATUSES.find((s) => s === v.status);
  expectShape(status !== undefined);
  return { counts: toCounts(v.counts), errorCode: textOrNull(v.error_code), finishedAt, queued: v.queued, status };
}

export function toNotionPlan(v: unknown): NotionPlan {
  expectShape(isRecord(v) && typeof v.available === "boolean" && typeof v.enabled === "boolean" && typeof v.connected_by_me === "boolean");
  return {
    available: v.available,
    parentPageId: textOrNull(v.parent_page_id),
    parentPageTitle: textOrNull(v.parent_page_title),
    parentPageUrl: textOrNull(v.parent_page_url),
    databaseUrl: textOrNull(v.database_url),
    enabled: v.enabled,
    connectedByName: textOrNull(v.connected_by_name),
    connectedByMe: v.connected_by_me,
    sync: toSync(v.sync),
  };
}

export function toNotionPlanAccess(v: unknown): NotionPlanAccess {
  expectShape(isRecord(v));
  if (v.result === "ok") {
    expectShape(isString(v.parent_page_title));
    return { result: "ok", parentPageTitle: v.parent_page_title };
  }
  expectShape(v.result === "failed" && isString(v.reason) && v.reason !== "");
  return { result: "failed", reason: v.reason };
}

export function createHttpNotionPlan(): NotionPlanPort {
  return {
    get: async (projectId) => {
      const body = await guard(() => getNotionPlan(projectId));
      return checked(() => toNotionPlan(body));
    },
    save: async (projectId, input) => {
      const body = await guard(() => putNotionPlan(projectId, input.link, input.enabled));
      return checked(() => toNotionPlan(body));
    },
    checkAccess: async (projectId, link) => {
      const body = await guard(() => checkNotionPlanAccess(projectId, link));
      return checked(() => toNotionPlanAccess(body));
    },
  };
}
