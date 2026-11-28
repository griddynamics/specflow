// docs/jira-sync/api.md#web-routes, ui.md#ports, oauth.md#routes: the production `JiraPort` over the three Jira routes of a project and
// the caller's Atlassian connection. Every body is shape-checked (like `toMembersState`): any other shape is `invalid_response`. Failures
// are `DataError`s whose `detail.apiCode` is the API code; a failed access check is an answer.
import { authorizeAtlassian, checkJiraAccess, getAtlassianConnection, getJira, putJira } from "@/lib/api";
import type {
  AtlassianConnection,
  JiraAccess,
  JiraActing,
  JiraAccessField,
  JiraPort,
  JiraSettings,
  JiraSync,
  JiraSyncProgress,
  JiraSyncState,
  JiraSyncStep,
} from "@/lib/domain/jira";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const STATES: readonly JiraSyncState[] = ["idle", "running", "succeeded", "partial", "failed", "skipped"];
const STEPS: readonly JiraSyncStep[] = ["issues", "links", "order"];
const FIELDS: readonly JiraAccessField[] = ["jira_project", "epic_key"];
const ACTING: readonly JiraActing["status"][] = ["active", "expired", "missing"];
const MODES: readonly NonNullable<AtlassianConnection["mode"]>[] = ["oauth", "token"];
const CONNECTED: readonly NonNullable<AtlassianConnection["status"]>[] = ["active", "expired"];

const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || isString(v));
  return v;
};

/** The Jira site as a link base: `https:` only, the origin (so no trailing slash); anything else is no site (the Epic key shows as text). */
function siteOf(v: unknown): string | null {
  const text = textOrNull(v);
  return text !== null && URL.canParse(text) && new URL(text).protocol === "https:" ? new URL(text).origin : null;
}

/** `progress`: missing or null (older servers, queued, finished) is none; anything but {step, done, total} with whole counts is invalid. */
function toProgress(v: unknown): JiraSyncProgress | null {
  if (v === undefined || v === null) return null;
  expectShape(isRecord(v));
  const step = STEPS.find((s) => s === v.step);
  expectShape(step !== undefined && Number.isSafeInteger(v.done) && Number.isSafeInteger(v.total));
  const done = v.done as number;
  const total = v.total as number;
  expectShape(done >= 0 && total >= 0);
  return { step, done, total };
}

function toSync(v: unknown): JiraSync {
  expectShape(isRecord(v));
  const status = STATES.find((s) => s === v.status);
  expectShape(status !== undefined);
  const finishedAt = textOrNull(v.finished_at);
  expectShape(finishedAt === null || !Number.isNaN(Date.parse(finishedAt)));
  return { status, finishedAt, errorCode: textOrNull(v.error_code), progress: toProgress(v.progress) };
}

/** `connection`: missing (an older server) or null is none; else {connected_by_name, connected_by_me, status}. */
function toActing(v: unknown): JiraActing | null {
  if (v === undefined || v === null) return null;
  expectShape(isRecord(v) && typeof v.connected_by_me === "boolean");
  const status = ACTING.find((s) => s === v.status);
  expectShape(status !== undefined);
  return { connectedByName: textOrNull(v.connected_by_name), connectedByMe: v.connected_by_me, status };
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
    connection: toActing(v.connection),
  };
}

/** `GET /integrations/atlassian` and the callback's `connection`. A connected answer has a status; a time that is not a date is invalid. */
export function toAtlassianConnection(v: unknown): AtlassianConnection {
  expectShape(isRecord(v) && typeof v.connected === "boolean");
  const mode = v.mode === null ? null : MODES.find((m) => m === v.mode);
  const status = v.status === null ? null : CONNECTED.find((s) => s === v.status);
  expectShape(mode !== undefined && status !== undefined && (status !== null) === v.connected);
  const when = (t: unknown) => {
    const text = textOrNull(t);
    expectShape(text === null || !Number.isNaN(Date.parse(text)));
    return text;
  };
  return {
    mode,
    connected: v.connected,
    status,
    accountName: textOrNull(v.account_name),
    connectedAt: when(v.connected_at),
    lastUsedAt: when(v.last_used_at),
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
    connection: async () => {
      const body = await guard(() => getAtlassianConnection());
      return checked(() => toAtlassianConnection(body));
    },
    authorize: async (returnTo) => {
      const body = await guard(() => authorizeAtlassian(returnTo));
      return checked(() => {
        expectShape(isRecord(body) && isString(body.url) && URL.canParse(body.url) && new URL(body.url).protocol === "https:");
        return body.url;
      });
    },
  };
}
