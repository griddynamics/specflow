// docs/github/INDEX.md: the rules of the GitHub tab, free of React. The caller's connection row, who may add
// repositories, the clone status of a repository (word, tone, facts, reason), the picker's list, the `?github=` toast and what a failed
// call means for the screen.
import { matchesQuery } from "@/components/ui/SearchField";
import { COPY } from "@/lib/copy";
import { isDataError } from "@/lib/domain/errors";
import type { CloneState, GitHubConnection, GitHubRepository, ProjectRepositories, ProjectRepository } from "@/lib/domain/github";
import { SETTINGS_COPY } from "./settings-copy";

/** The caller's connection row: connected (with the GitHub login), expired or none. */
export type GitHubRow = { kind: "connected"; login: string } | { kind: "expired" } | { kind: "none" };

export function githubRowOf(c: GitHubConnection): GitHubRow {
  if (c.status === "active") return { kind: "connected", login: c.login ?? "" };
  return c.status === "expired" ? { kind: "expired" } : { kind: "none" };
}

/** Why an owner cannot add a repository now (no active connection), or null when they can. */
export function addGate(row: GitHubRow): string | null {
  if (row.kind === "connected") return null;
  return row.kind === "expired" ? SETTINGS_COPY.reposNeedReconnect : SETTINGS_COPY.reposNeedConnect;
}

export type Tone = "neutral" | "pending" | "success" | "danger";

const CLONE_WORD: Record<CloneState, string> = {
  pending: SETTINGS_COPY.clonePending,
  cloning: SETTINGS_COPY.cloneRunning,
  cloned: SETTINGS_COPY.cloneDone,
  failed: SETTINGS_COPY.cloneFailedWord,
};
const CLONE_TONE: Record<CloneState, Tone> = { pending: "pending", cloning: "pending", cloned: "success", failed: "danger" };

const FAILURE_TEXT: Record<string, string> = {
  github_not_connected: COPY.cloneNotConnected,
  github_reconnect_required: COPY.cloneReconnect,
  github_repo_not_accessible: COPY.cloneNotAccessible,
  github_repo_too_large: COPY.cloneTooLarge,
  github_unreachable: COPY.cloneUnreachable,
  github_auth_failed: COPY.cloneAuthFailed,
  github_clone_failed: COPY.cloneFailed,
  github_clone_timeout: COPY.cloneTimeout,
  storage_failed: COPY.cloneStorageFailed,
};

/** The text of a clone `failure_code`; an unknown code is the generic one. */
export const failureText = (code: string): string => (Object.hasOwn(FAILURE_TEXT, code) ? FAILURE_TEXT[code] : COPY.cloneUnexpected);

const NUMBER = new Intl.NumberFormat("en");
const UNITS = ["B", "KB", "MB", "GB"] as const;

/** 1536 -> "1.5 KB"; whole bytes below 1 KB; one decimal below 10 of a unit, none above. */
export function bytesText(n: number): string {
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || value >= 10 ? 0 : 1;
  return `${value.toFixed(digits)} ${UNITS[unit]}`;
}

/** The first 7 characters of a commit SHA, as GitHub shows it. */
export const shortSha = (sha: string): string => sha.slice(0, 7);

/** "1,204 files" (one file: "1 file"). */
export const filesText = (n: number): string => `${NUMBER.format(n)} ${n === 1 ? "file" : SETTINGS_COPY.files}`;

/** The facts of the last good snapshot: "abc1234 · 1,204 files · 3.2 MB"; empty when there is none. */
export function snapshotFacts(r: ProjectRepository): string {
  const parts: string[] = [];
  if (r.headSha !== null) parts.push(shortSha(r.headSha));
  if (r.fileCount !== null) parts.push(filesText(r.fileCount));
  const bytes = r.snapshotBytes ?? r.sizeBytes;
  if (bytes !== null) parts.push(bytesText(bytes));
  return parts.join(" · ");
}

/** The status of a repository row: the word and tone of its badge, the snapshot facts and, after a failure, the reason. */
export interface CloneView {
  word: string;
  tone: Tone;
  facts: string;
  reason: string | null;
}

export function cloneViewOf(r: ProjectRepository): CloneView {
  const facts = snapshotFacts(r);
  return {
    word: CLONE_WORD[r.cloneState],
    tone: CLONE_TONE[r.cloneState],
    // After a failure the facts are the last good copy's (kept by the API), named as such.
    facts: r.cloneState === "failed" && facts !== "" ? `${SETTINGS_COPY.lastGood}: ${facts}` : facts,
    reason: r.cloneState === "failed" ? failureText(r.failureCode ?? "") : null,
  };
}

/** Who added the repository, for its sub line. */
export function addedByText(r: ProjectRepository): string | null {
  if (r.addedByMe) return SETTINGS_COPY.addedByYou;
  return r.addedByName === null ? null : `${SETTINGS_COPY.addedBy} ${r.addedByName}`;
}

/** True while a clone check waits or runs: the tab reloads the list then. */
export const isCloning = (list: ProjectRepositories | null): boolean =>
  list?.repositories.some((r) => r.cloneState === "pending" || r.cloneState === "cloning") ?? false;

/** The list reloads this often while a check waits or runs. */
export const POLL_MS = 3000;

/** Calls `load` every `ms` while `running`; returns the stop function (none when not running). */
export function startPolling(running: boolean, load: () => void, ms = POLL_MS): (() => void) | undefined {
  if (!running) return undefined;
  const timer = setInterval(load, ms);
  return () => {
    clearInterval(timer);
  };
}

/** A row of the Add dialog: the repository and whether it can be added (`blocked` names why not). */
export interface PickRow {
  repo: GitHubRepository;
  blocked: "added" | "too_large" | null;
}

/** The repositories that match `query` (name, anywhere, any case), each with why it cannot be added; added ones last. */
export function pickRowsOf(repos: readonly GitHubRepository[], query: string, addedIds: ReadonlySet<number>): PickRow[] {
  const q = query.trim();
  const rows = repos
    .filter((repo) => q === "" || matchesQuery(repo.fullName, q))
    .map((repo): PickRow => {
      if (addedIds.has(repo.githubRepoId)) return { repo, blocked: "added" };
      return { repo, blocked: repo.tooLarge ? "too_large" : null };
    });
  const rank = (r: PickRow) => (r.blocked === null ? 0 : 1);
  return rows.toSorted((a, b) => rank(a) - rank(b));
}

const TOAST: Record<string, string> = {
  github_auth_failed: COPY.githubAuthFailed,
  github_consent_denied: COPY.githubConsentDenied,
  github_state_invalid: COPY.githubStateInvalid,
  github_code_invalid: COPY.githubCodeInvalid,
  github_unreachable: COPY.githubUnreachable,
  github_unavailable: COPY.githubUnavailable,
};

/** The one toast of `?github=<code>` (a return route's answer): `connected` and `installed` succeed, any other value is a failure. */
export function githubToast(code: string): { title: string; tone: "success" | "danger" } {
  if (code === "connected") return { title: COPY.githubConnected, tone: "success" };
  if (code === "installed") return { title: COPY.githubInstalled, tone: "success" };
  return { title: Object.hasOwn(TOAST, code) ? TOAST[code] : COPY.githubUnexpected, tone: "danger" };
}

/**
 * What a failed call means for the screen: `gone` (the project is gone: go home), `reload` (the connection or the server changed
 * meanwhile: show the text and read the tab again) or a plain toast.
 */
export type GitHubFailure = { kind: "gone" } | { kind: "reload"; text: string } | { kind: "toast"; text: string };

const RELOAD_TEXT: Record<string, string> = {
  github_unavailable: COPY.githubUnavailable,
  github_not_connected: COPY.githubNotConnected,
  github_reconnect_required: COPY.githubReconnectRequired,
  github_repo_not_accessible: COPY.githubRepoNotAccessible,
  repository_exists: COPY.githubRepositoryExists,
  repository_not_found: COPY.githubActionFailed,
};
const TOAST_TEXT: Record<string, string> = {
  github_unreachable: COPY.githubUnreachable,
  github_auth_failed: COPY.githubAuthFailed,
  github_repo_too_large: COPY.githubRepoTooLarge,
};

export function githubFailureOf(e: unknown): GitHubFailure {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  if (code === "project_not_found") return { kind: "gone" };
  if (Object.hasOwn(RELOAD_TEXT, code)) return { kind: "reload", text: RELOAD_TEXT[code] };
  return { kind: "toast", text: Object.hasOwn(TOAST_TEXT, code) ? TOAST_TEXT[code] : COPY.githubActionFailed };
}

/** The list with `repo` in place of the row of the same id (a new one goes first). */
export function withRepository(list: ProjectRepositories, repo: ProjectRepository): ProjectRepositories {
  const has = list.repositories.some((r) => r.id === repo.id);
  return {
    ...list,
    repositories: has ? list.repositories.map((r) => (r.id === repo.id ? repo : r)) : [repo, ...list.repositories],
  };
}

/** The list without the repository `id` (removed). */
export const withoutRepository = (list: ProjectRepositories, id: string): ProjectRepositories => ({
  ...list,
  repositories: list.repositories.filter((r) => r.id !== id),
});
