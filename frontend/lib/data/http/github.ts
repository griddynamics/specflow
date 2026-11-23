// plans/GITHUB-INTEGRATION/contract.md#api: the production `GitHubPort` over the caller's GitHub connection and the repositories of a
// project. Every body is shape-checked (like the Jira port): any other shape is `invalid_response`. Failures are `DataError`s whose
// `detail.apiCode` is the API code. URLs the browser opens or follows must be `https:` on github.com.
import {
  addProjectRepository,
  authorizeGitHub,
  cloneProjectRepository,
  disconnectGitHub,
  getGitHubConnection,
  installGitHub,
  listGitHubRepositories,
  listProjectRepositories,
  removeProjectRepository,
} from "@/lib/api";
import type {
  CloneState,
  GitHubConnection,
  GitHubPort,
  GitHubRepository,
  GitHubRepositoryList,
  ProjectRepositories,
  ProjectRepository,
} from "@/lib/domain/github";
import { checked, expectShape, guard, isRecord, isString } from "./errors";

const CONNECTED: readonly NonNullable<GitHubConnection["status"]>[] = ["active", "expired"];
const CLONE_STATES: readonly CloneState[] = ["pending", "cloning", "cloned", "failed"];
const GITHUB_HOST = "github.com";

const textOrNull = (v: unknown): string | null => {
  expectShape(v === null || isString(v));
  return v;
};
const whenOrNull = (v: unknown): string | null => {
  const text = textOrNull(v);
  expectShape(text === null || !Number.isNaN(Date.parse(text)));
  return text;
};
const countOrNull = (v: unknown): number | null => {
  if (v === null) return null;
  expectShape(Number.isSafeInteger(v) && (v as number) >= 0);
  return v as number;
};
const idOf = (v: unknown): number => {
  expectShape(Number.isSafeInteger(v) && (v as number) > 0);
  return v as number;
};

/** An `https:` URL on github.com (or one of its subdomains); anything else is not the contract. */
function githubUrl(v: unknown): string {
  expectShape(isString(v) && URL.canParse(v));
  const url = new URL(v);
  expectShape(url.protocol === "https:" && (url.hostname === GITHUB_HOST || url.hostname.endsWith(`.${GITHUB_HOST}`)));
  return url.href;
}

export function toGitHubConnection(v: unknown): GitHubConnection {
  expectShape(isRecord(v) && typeof v.enabled === "boolean" && typeof v.connected === "boolean");
  const status = v.status === null ? null : CONNECTED.find((s) => s === v.status);
  expectShape(status !== undefined && (status !== null) === v.connected);
  return {
    enabled: v.enabled,
    connected: v.connected,
    status,
    login: textOrNull(v.login),
    connectedAt: whenOrNull(v.connected_at),
    lastUsedAt: whenOrNull(v.last_used_at),
    manageUrl: v.manage_url === null ? null : githubUrl(v.manage_url),
  };
}

export function toGitHubRepository(v: unknown): GitHubRepository {
  expectShape(isRecord(v) && isString(v.full_name) && v.full_name !== "" && typeof v.private === "boolean");
  expectShape(typeof v.too_large === "boolean");
  return {
    githubRepoId: idOf(v.github_repo_id),
    fullName: v.full_name,
    htmlUrl: githubUrl(v.html_url),
    private: v.private,
    defaultBranch: textOrNull(v.default_branch),
    sizeKb: countOrNull(v.size_kb) ?? 0,
    tooLarge: v.too_large,
  };
}

export function toGitHubRepositoryList(v: unknown): GitHubRepositoryList {
  expectShape(isRecord(v) && Array.isArray(v.repositories) && typeof v.truncated === "boolean");
  return { repositories: v.repositories.map(toGitHubRepository), truncated: v.truncated };
}

export function toProjectRepository(v: unknown): ProjectRepository {
  expectShape(isRecord(v) && isString(v.id) && v.id !== "" && isString(v.full_name) && v.full_name !== "");
  expectShape(typeof v.private === "boolean" && typeof v.added_by_me === "boolean");
  const cloneState = CLONE_STATES.find((s) => s === v.clone_state);
  expectShape(cloneState !== undefined);
  const failureCode = textOrNull(v.failure_code);
  expectShape((cloneState === "failed") === (failureCode !== null));
  const createdAt = whenOrNull(v.created_at);
  expectShape(createdAt !== null);
  return {
    id: v.id,
    githubRepoId: idOf(v.github_repo_id),
    fullName: v.full_name,
    htmlUrl: githubUrl(v.html_url),
    private: v.private,
    addedByName: textOrNull(v.added_by_name),
    addedByMe: v.added_by_me,
    cloneState,
    failureCode,
    defaultBranch: textOrNull(v.default_branch),
    headSha: textOrNull(v.head_sha),
    fileCount: countOrNull(v.file_count),
    sizeBytes: countOrNull(v.size_bytes),
    snapshotBytes: countOrNull(v.snapshot_bytes),
    checkedAt: whenOrNull(v.checked_at),
    clonedAt: whenOrNull(v.cloned_at),
    createdAt,
  };
}

export function toProjectRepositories(v: unknown): ProjectRepositories {
  expectShape(isRecord(v) && typeof v.enabled === "boolean" && typeof v.can_manage === "boolean" && Array.isArray(v.repositories));
  return { enabled: v.enabled, canManage: v.can_manage, repositories: v.repositories.map(toProjectRepository) };
}

/** `{url}` with a GitHub URL (the authorize page or the app's install page). */
const urlOf = (body: unknown): string => {
  expectShape(isRecord(body));
  return githubUrl(body.url);
};

export function createHttpGitHub(): GitHubPort {
  return {
    connection: async () => {
      const body = await guard(() => getGitHubConnection());
      return checked(() => toGitHubConnection(body));
    },
    authorize: async (returnTo) => {
      const body = await guard(() => authorizeGitHub(returnTo));
      return checked(() => urlOf(body));
    },
    install: async (returnTo) => {
      const body = await guard(() => installGitHub(returnTo));
      return checked(() => urlOf(body));
    },
    disconnect: async () => {
      await guard(() => disconnectGitHub());
    },
    available: async () => {
      const body = await guard(() => listGitHubRepositories());
      return checked(() => toGitHubRepositoryList(body));
    },
    list: async (projectId) => {
      const body = await guard(() => listProjectRepositories(projectId));
      return checked(() => toProjectRepositories(body));
    },
    add: async (projectId, githubRepoId) => {
      const body = await guard(() => addProjectRepository(projectId, githubRepoId));
      return checked(() => toProjectRepository(body));
    },
    remove: async (projectId, repositoryId) => {
      await guard(() => removeProjectRepository(projectId, repositoryId));
    },
    clone: async (projectId, repositoryId) => {
      const body = await guard(() => cloneProjectRepository(projectId, repositoryId));
      return checked(() => toProjectRepository(body));
    },
  };
}
