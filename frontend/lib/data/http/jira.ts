// docs/jira-sync/api.md#web-routes, ui.md#ports: the production `JiraPort` over the three Jira routes of a project. Every body is
// shape-checked (like `toMembersState`): any other shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is
// the API code; a failed access check is an answer.
import { checkJiraAccess, getJira, putJira } from "@/lib/api";
import type { JiraAccess, JiraAccessField, JiraPort, JiraSettings, JiraSync, JiraSyncState } from "@/lib/domain/jira";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const STATES: readonly JiraSyncState[] = ["idle", "running", "succeeded", "partial", "failed", "skipped"];
const FIELDS: readonly JiraAccessField[] = ["jira_project", "epic_key"];

const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || isString(v));
  return v;
};

/** The Jira site as a link base: `https:` only, the origin (so no trailing slash); anything else is no site (the Epic key shows as text). */
function siteOf(v: unknown): string | null {
  const text = textOrNull(v);
  return text !== null && URL.canParse(text) && new URL(text).protocol === "https:" ? new URL(text).origin : null;
}

function toSync(v: unknown): JiraSync {
  expectShape(isRecord(v));
  const status = STATES.find((s) => s === v.status);
  expectShape(status !== undefined);
  const finishedAt = textOrNull(v.finished_at);
  expectShape(finishedAt === null || !Number.isNaN(Date.parse(finishedAt)));
  return { status, finishedAt, errorCode: textOrNull(v.error_code) };
}

export function toJiraSettings(v: unknown): JiraSettings {
  expectShape(isRecord(v) && typeof v.available === "boolean" && typeof v.editable === "boolean" && typeof v.enabled === "boolean");
  return {
    available: v.available,
    editable: v.editable,
    siteUrl: siteOf(v.site_url),
    jiraProject: textOrNull(v.jira_project),
    epicKey: textOrNull(v.epic_key),
    createdEpicKey: textOrNull(v.created_epic_key),
    enabled: v.enabled,
    sync: v.sync === null ? null : toSync(v.sync),
  };
}

const fieldOf = (v: unknown): JiraAccessField | null => {
  if (v === null) return null;
  const field = FIELDS.find((f) => f === v);
  expectShape(field !== undefined);
  return field;
};

export function toJiraAccess(v: unknown): JiraAccess {
  expectShape(isRecord(v));
  if (v.result === "ok") {
    expectShape(isString(v.project_name));
    return { result: "ok", projectName: v.project_name, epicSummary: textOrNull(v.epic_summary) };
  }
  expectShape(v.result === "failed" && isString(v.reason) && v.reason !== "");
  return { result: "failed", reason: v.reason, field: fieldOf(v.field) };
}

export function createHttpJira(): JiraPort {
  return {
    get: async (projectId) => {
      const body = await guard(() => getJira(projectId));
      return checked(() => toJiraSettings(body));
    },
    save: async (projectId, input) => {
      const body = await guard(() => putJira(projectId, input.jiraProject, input.epicKey, input.enabled));
      return checked(() => toJiraSettings(body));
    },
    checkAccess: async (projectId, target) => {
      const body = await guard(() => checkJiraAccess(projectId, target.jiraProject, target.epicKey));
      return checked(() => toJiraAccess(body));
    },
  };
}
