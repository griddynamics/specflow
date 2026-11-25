// docs/github/projects.md: the production `GitHubProjectsPort` over `/projects/{pid}/github-projects`. Every body is shape-checked: any other
// shape is `invalid_response`. Failures are `DataError`s whose `detail.apiCode` is the API code; a failed access check is an answer.
import { checkGitHubProjectsAccess, getGitHubProjects, listGitHubProjects, putGitHubProjects } from "@/lib/api";
import type {
  GitHubProjectChoice,
  GitHubProjectCounts,
  GitHubProjectList,
  GitHubProjects,
  GitHubProjectsAccess,
  GitHubProjectsField,
  GitHubProjectsPort,
  GitHubProjectSync,
  GitHubSyncStatus,
} from "@/lib/domain/github-projects";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const STATUSES: readonly GitHubSyncStatus[] = ["idle", "running", "succeeded", "partial", "failed", "skipped"];
const FIELDS: readonly GitHubProjectsField[] = ["repository", "project"];

const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || v === undefined || isString(v));
  return v ?? null;
};

const count = (v: unknown): number => {
  expectShape(typeof v === "number" && Number.isSafeInteger(v) && v >= 0);
  return v;
};

function toCounts(v: unknown): GitHubProjectCounts | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v));
  return {
    created: count(v.created),
    updated: count(v.updated),
    commented: count(v.commented),
    removed: count(v.removed),
    ranked: count(v.ranked),
    unchanged: count(v.unchanged),
    failed: count(v.failed),
  };
}

function toSync(v: unknown): GitHubProjectSync | null {
  if (v === null || v === undefined) return null;
  expectShape(isRecord(v) && typeof v.queued === "boolean");
  const status = STATUSES.find((s) => s === v.status);
  expectShape(status !== undefined);
  const finishedAt = textOrNull(v.finished_at);
  expectShape(finishedAt === null || !Number.isNaN(Date.parse(finishedAt)));
  return { status, queued: v.queued, finishedAt, errorCode: textOrNull(v.error_code), counts: toCounts(v.counts) };
}

export function toGitHubProjects(v: unknown): GitHubProjects {
  expectShape(isRecord(v) && typeof v.available === "boolean" && typeof v.enabled === "boolean" && typeof v.connected_by_me === "boolean");
  return {
    available: v.available,
    repositoryId: textOrNull(v.repository_id),
    repository: textOrNull(v.repository),
    projectNodeId: textOrNull(v.project_node_id),
    projectTitle: textOrNull(v.project_title),
    projectUrl: textOrNull(v.project_url),
    enabled: v.enabled,
    connectedByName: textOrNull(v.connected_by_name),
    connectedByMe: v.connected_by_me,
    sync: toSync(v.sync),
  };
}

function toChoice(v: unknown): GitHubProjectChoice {
  expectShape(isRecord(v) && isString(v.node_id) && isString(v.title) && isString(v.url) && Number.isSafeInteger(v.number));
  return { nodeId: v.node_id, number: v.number as number, title: v.title, url: v.url };
}

export function toGitHubProjectList(v: unknown): GitHubProjectList {
  expectShape(isRecord(v) && Array.isArray(v.projects));
  return { organization: textOrNull(v.organization), projects: v.projects.map(toChoice) };
}

export function toGitHubProjectsAccess(v: unknown): GitHubProjectsAccess {
  expectShape(isRecord(v));
  if (v.result === "ok") {
    expectShape(isString(v.organization));
    return { result: "ok", organization: v.organization, projectTitle: textOrNull(v.project_title) };
  }
  expectShape(v.result === "failed" && isString(v.reason) && v.reason !== "");
  const field = v.field === null || v.field === undefined ? null : FIELDS.find((f) => f === v.field);
  expectShape(field !== undefined);
  return { result: "failed", reason: v.reason, field };
}

export function createHttpGitHubProjects(): GitHubProjectsPort {
  return {
    get: async (projectId) => {
      const body = await guard(() => getGitHubProjects(projectId));
      return checked(() => toGitHubProjects(body));
    },
    projects: async (projectId, repositoryId) => {
      const body = await guard(() => listGitHubProjects(projectId, repositoryId));
      return checked(() => toGitHubProjectList(body));
    },
    save: async (projectId, input) => {
      const body = await guard(() => putGitHubProjects(projectId, input.repositoryId, input.project, input.enabled));
      return checked(() => toGitHubProjects(body));
    },
    checkAccess: async (projectId, target) => {
      const body = await guard(() => checkGitHubProjectsAccess(projectId, target.repositoryId, target.project));
      return checked(() => toGitHubProjectsAccess(body));
    },
  };
}
