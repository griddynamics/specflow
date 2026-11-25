// The words and rules of the Linear plan destination (plans/LINEAR/LINEAR-PLAN.md, stream S3), free of React: what a failed check or save says
// (the closed codes of the API), what a passed check says, and what a node says when its last pass ended on a code or finished.
import type { LinearPlan, LinearPlanAccess, LinearPlanCounts, LinearPlanTarget } from "@/lib/domain/linear-plan";
import { DataError, isDataError } from "@/lib/domain/errors";
import { V2_COPY } from "../v2-copy";

const RECONNECT_ON_PAGE = "Connect Linear to this project on the Integrations page.";

/** The words of the form and the node that no shared copy file holds. */
export const LINEAR_PLAN_COPY = {
  team: "Team",
  project: "Project",
  pickTeam: "Pick a team",
  pickProject: "Pick a project",
  newProject: "Create “{name}”",
  newProjectHint: "Graphico creates a Linear project with this name in the team.",
  loadingTeams: "Reading the teams…",
  loadingProjects: "Reading the projects…",
  teamsFailed: "The teams of the Linear account could not be read.",
  projectsFailed: "The projects of the team could not be read.",
  noTeams: "The Linear account sees no team. Ask a workspace admin to add it to one.",
  stopped: "Stopped",
  off: "Sync is off",
  synced: "In sync",
  exports: "Stories become issues and tasks sub-issues in the Linear project, kept up to date.",
  partial: "The last pass could not do everything; the next one tries again.",
  openProject: "Open in Linear",
} as const;

const FAILURE: Record<string, string> = {
  linear_not_connected: RECONNECT_ON_PAGE,
  linear_reconnect_required: "The Linear connection expired. Connect Linear again on the Integrations page.",
  linear_unreachable: "Linear could not be reached. Try again later.",
  linear_forbidden: "The connected Linear account is not allowed to do this in Linear.",
  linear_not_found: "The Linear object does not exist or the account cannot see it.",
  linear_rate_limited: "Linear is limiting requests. Try again in a moment.",
  linear_invalid: "Linear did not accept this destination.",
  linear_unavailable: "Linear is not available on this server.",
  linear_team_not_found: "The Linear team does not exist, or the connected account cannot see it.",
  linear_project_not_found: "The Linear project does not exist, or the connected account cannot see it.",
};

/** What a pass ended on that is not a failure of the connection: the pass waits for the plan, was overtaken or will be repeated. */
const PASS_TEXT: Record<string, string> = {
  linear_rate_limited: "Linear asked to wait. Graphico tries again by itself.",
  linear_invalid: "Linear did not accept an item of the plan.",
  linear_issue_gone: "An issue was deleted in Linear. It is not created again.",
  linear_estimate_refused: "The team's estimate scale refused some points. Those issues have no estimate.",
  linear_project_not_found: "The Linear project does not exist any more. Pick another one.",
  linear_not_configured: "The destination is not set up.",
  no_plan: "Waits for the plan",
  plan_applying: "The plan is being applied",
  target_changed: "The destination changed; the next pass uses the new one",
  unexpected_error: "The last pass failed for a reason Graphico does not know. The next pass tries again.",
};

/** What a failed check or save says, from the API's closed code; anything else is the general failure. */
export function linearPlanFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

/** What a node says of the code its last pass ended on (`sync.error_code`). */
export function linearPassText(code: string): string {
  if (Object.hasOwn(PASS_TEXT, code)) return PASS_TEXT[code];
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

/** The codes that need the connection of the member who connected Linear, not a retry: the node offers Reconnect Linear. */
export const LINEAR_RECONNECT = new Set(["linear_not_connected", "linear_reconnect_required"]);

/** A failed access check as the refusal `useCheckAdd` shows: the reason is the API code the words are found by. */
export function linearAccessOutcome(access: LinearPlanAccess): { teamKey: string; projectName: string | null } {
  if (access.result === "ok") return { teamKey: access.teamKey, projectName: access.projectName };
  throw new DataError("validation", false, { apiCode: access.reason });
}

/** The line under a passed check: the team, and the project that is used or the one that will be made. */
export function linearPlanCheckText(
  found: { teamKey: string; projectName: string | null },
  target: LinearPlanTarget,
  newName: string | null,
): string {
  const team = `Team ${found.teamKey}.`;
  if (target.projectId === null) return newName === null ? team : `${team} A project named “${newName}” will be created.`;
  return found.projectName === null ? team : `${team} Project “${found.projectName}”.`;
}

const plural = (n: number): string => `${String(n)} ${n === 1 ? "issue" : "issues"}`;

/** The meta line of a node that finished a pass: the issues made, or that nothing changed. */
export function linearSyncedMeta(counts: LinearPlanCounts): string {
  return counts.created > 0 ? `${plural(counts.created)} · Linear` : LINEAR_PLAN_COPY.synced;
}

/** The counts of the last pass in one line, for the node's dialog. */
export function linearCountsText(c: LinearPlanCounts): string {
  const parts = [
    `${String(c.created)} created`,
    `${String(c.updated)} updated`,
    `${String(c.commented)} commented`,
    `${String(c.failed)} failed`,
  ];
  return `Last pass: ${parts.join(" · ")}.`;
}

/** The sentence under the node's state: what it does, how the last pass went and where the project is. */
export function linearDetailText(plan: LinearPlan): string {
  const sync = plan.sync;
  const parts: string[] = [LINEAR_PLAN_COPY.exports];
  if (sync?.status === "partial") parts.push(LINEAR_PLAN_COPY.partial);
  if (sync?.counts != null) parts.push(linearCountsText(sync.counts));
  if (sync?.errorCode != null && sync.status !== "failed") parts.push(linearPassText(sync.errorCode));
  return parts.join(" ");
}
