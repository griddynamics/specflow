// docs/jira-sync/gaps.md: the production `JiraGapsPort` over `/projects/{pid}/jira-gaps`. Every body is shape-checked: any other shape is
// `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code; a failed access check is an answer.
import { checkJiraGapsAccess, getJiraGaps, putJiraGaps } from "@/lib/api";
import type { JiraGapGrouping, JiraGaps, JiraGapsPort } from "@/lib/domain/jira-gaps";
import { toJiraAccess } from "./jira";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const GROUPINGS: readonly JiraGapGrouping[] = ["one", "area"];

const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || v === undefined || isString(v));
  return v ?? null;
};

const count = (v: unknown): number => {
  expectShape(typeof v === "number" && Number.isSafeInteger(v) && v >= 0);
  return v;
};

export function toJiraGaps(v: unknown): JiraGaps {
  expectShape(isRecord(v) && typeof v.available === "boolean" && typeof v.enabled === "boolean");
  const grouping = v.grouping === null || v.grouping === undefined ? null : GROUPINGS.find((g) => g === v.grouping);
  expectShape(grouping !== undefined);
  const polled = textOrNull(v.last_polled_at);
  expectShape(polled === null || !Number.isNaN(Date.parse(polled)));
  return {
    available: v.available,
    jiraProject: textOrNull(v.jira_project),
    grouping,
    enabled: v.enabled,
    storyKey: textOrNull(v.story_key),
    storyUrl: textOrNull(v.story_url),
    posted: count(v.posted),
    answered: count(v.answered),
    lastPolledAt: polled,
    errorCode: textOrNull(v.error_code),
    pollSeconds: count(v.poll_seconds),
  };
}

export function createHttpJiraGaps(): JiraGapsPort {
  return {
    get: async (projectId) => {
      const body = await guard(() => getJiraGaps(projectId));
      return checked(() => toJiraGaps(body));
    },
    save: async (projectId, input) => {
      const body = await guard(() => putJiraGaps(projectId, input.jiraProject, input.grouping, input.enabled));
      return checked(() => toJiraGaps(body));
    },
    checkAccess: async (projectId, jiraProject) => {
      const body = await guard(() => checkJiraGapsAccess(projectId, jiraProject));
      return checked(() => toJiraAccess(body));
    },
  };
}
