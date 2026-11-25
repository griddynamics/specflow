// The words and rules of the GitHub Projects destination (docs/github/projects.md), free of React: what a failed check or save says (the closed
// codes of the API), what a passed check says, and what a node says when its last pass ended on a code.
import { COPY } from "@/lib/copy";
import type { GitHubProjectsAccess, GitHubProjectsTarget } from "@/lib/domain/github-projects";
import { DataError, isDataError } from "@/lib/domain/errors";
import { V2_COPY } from "../v2-copy";

/** What to approve when the installation lacks the new permissions: the one thing a member cannot guess. */
const PERMISSIONS =
  "Approve the new permissions of the Specflow GitHub App for this installation: Issues (read and write) and Organization projects (read and write). An organization owner approves them on GitHub.";

const FAILURE: Record<string, string> = {
  github_not_connected: COPY.githubNotConnected,
  github_reconnect_required: COPY.githubReconnectRequired,
  github_unreachable: COPY.githubUnreachable,
  github_auth_failed: COPY.githubAuthFailed,
  github_unavailable: COPY.githubUnavailable,
  github_repo_not_accessible: COPY.githubRepoNotAccessible,
  github_not_an_organization: "This repository belongs to a person, not an organization. Only organization projects can be used.",
  github_repo_read_only: "The connected GitHub account cannot push to this repository.",
  github_permissions_missing: PERMISSIONS,
  github_project_not_found: "There is no open project with this title in the organization, or the account cannot see it.",
  repository_not_found: "This repository is not in the project any more.",
};

/** Codes a pass ends on that are not a failure of the connection: the pass is waiting for the plan or was overtaken. */
const PASS_TEXT: Record<string, string> = {
  github_refused: "GitHub refused a change. The next pass tries again.",
  github_item_invalid: "GitHub did not accept an item of the plan.",
  github_issue_gone: "An issue was deleted on GitHub. It is not created again.",
  github_projects_not_configured: "The destination is not set up.",
};

/** What a failed check or save says, from the API's closed code; anything else is the general failure. */
export function githubPlanFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

/** What a node says of the code its last pass ended on (`sync.error_code`). */
export function githubPassText(code: string): string {
  if (Object.hasOwn(PASS_TEXT, code)) return PASS_TEXT[code];
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

/** The codes that need the connection of the member who connected GitHub, not a retry: the node offers Reconnect GitHub. */
export const GITHUB_RECONNECT = new Set(["github_not_connected", "github_reconnect_required"]);

/** A failed access check as the refusal `useCheckAdd` shows: the reason is the API code the words are found by. */
export function accessOutcome(access: GitHubProjectsAccess): { organization: string; projectTitle: string | null } {
  if (access.result === "ok") return { organization: access.organization, projectTitle: access.projectTitle };
  throw new DataError("validation", false, { apiCode: access.reason });
}

/** The line under a passed check: the organization, and the project that is used or the one that will be made. */
export function githubPlanCheckText(found: { organization: string; projectTitle: string | null }, target: GitHubProjectsTarget): string {
  const org = `Organization ${found.organization}.`;
  if ("newTitle" in target.project) return `${org} A project named “${target.project.newTitle}” will be created.`;
  return found.projectTitle === null ? org : `${org} Project “${found.projectTitle}”.`;
}
