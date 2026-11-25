// docs/linear/gaps.md: the production `LinearGapsPort` over the three routes of `/projects/{pid}/linear-gaps`. Every body is shape-checked: any
// other shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code; a failed access check is an answer.
import { checkLinearGapsAccess, getLinearGaps, putLinearGaps } from "@/lib/api";
import type { LinearGapGrouping, LinearGaps, LinearGapsAccess, LinearGapsAccessField, LinearGapsPort } from "@/lib/domain/linear-gaps";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const GROUPINGS: readonly LinearGapGrouping[] = ["one", "area"];
const FIELDS: readonly LinearGapsAccessField[] = ["team_key", "linear_project"];

const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || v === undefined || isString(v));
  return v ?? null;
};

const count = (v: unknown): number => {
  expectShape(typeof v === "number" && Number.isSafeInteger(v) && v >= 0);
  return v;
};

export function toLinearGaps(v: unknown): LinearGaps {
  expectShape(isRecord(v) && typeof v.available === "boolean" && typeof v.enabled === "boolean");
  const grouping = v.grouping === null || v.grouping === undefined ? null : GROUPINGS.find((g) => g === v.grouping);
  expectShape(grouping !== undefined);
  return {
    available: v.available,
    enabled: v.enabled,
    teamKey: textOrNull(v.team_key),
    linearProjectId: textOrNull(v.linear_project_id),
    grouping,
    parentIdentifier: textOrNull(v.parent_identifier),
    parentUrl: textOrNull(v.parent_url),
    posted: count(v.posted),
    answered: count(v.answered),
    errorCode: textOrNull(v.error_code),
  };
}

export function toLinearGapsAccess(v: unknown): LinearGapsAccess {
  expectShape(isRecord(v));
  if (v.result === "ok") {
    expectShape(isString(v.team_name));
    return { result: "ok", teamName: v.team_name, linearProjectName: textOrNull(v.linear_project_name) };
  }
  expectShape(v.result === "failed" && isString(v.reason) && v.reason !== "");
  const field = v.field === null || v.field === undefined ? null : FIELDS.find((f) => f === v.field);
  expectShape(field !== undefined);
  return { result: "failed", reason: v.reason, field };
}

export function createHttpLinearGaps(): LinearGapsPort {
  return {
    get: async (projectId) => {
      const body = await guard(() => getLinearGaps(projectId));
      return checked(() => toLinearGaps(body));
    },
    save: async (projectId, input) => {
      const body = await guard(() => putLinearGaps(projectId, input.teamKey, input.linearProject, input.grouping, input.enabled));
      return checked(() => toLinearGaps(body));
    },
    checkAccess: async (projectId, input) => {
      const body = await guard(() => checkLinearGapsAccess(projectId, input.teamKey, input.linearProject));
      return checked(() => toLinearGapsAccess(body));
    },
  };
}
