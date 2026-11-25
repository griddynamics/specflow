// The words of the Linear gap channel: its form, its node and the closed codes its calls end on (docs/linear/gaps.md). Plain text, no React.

const ERRORS: Readonly<Record<string, string>> = {
  linear_not_connected: "Connect your Linear account first under Integrations.",
  linear_reconnect_required: "Your Linear connection expired. Connect Linear again under Integrations.",
  linear_forbidden: "Your Linear account is not allowed to post in this team.",
  linear_not_found: "Linear no longer has the team, project or issue.",
  linear_rate_limited: "Linear is limiting requests. Posting resumes with the next gap run.",
  linear_unreachable: "Linear could not be reached. Posting resumes with the next gap run.",
  linear_invalid: "Linear did not accept a question. Posting resumes with the next gap run.",
};

const REASONS: Readonly<Partial<Record<string, "team" | "project">>> = {
  linear_team_not_found: "team",
  linear_project_not_found: "project",
};

export const LINEAR_GAP_COPY = {
  readFailed: "The Linear gap channel could not be read. Check your connection and try again.",
  team: "Linear team key",
  teamHint: "The key of the team that gets the questions, for example ENG.",
  project: "Linear project (optional)",
  projectHint: "A project of that team, by name or link. Leave it empty to post in the team only.",
  grouping: "Sub-issues",
  groupingOne: "One per question",
  groupingArea: "One per work area, its questions numbered",
  note: "The open questions are posted as sub-issues of one issue. Answers come back as comments, as soon as Linear sends them.",
  teamLine: "Linear team",
  projectLine: "Linear project",
  saving: "Saving…",
  off: "Posting is off",
  stopped: "Stopped",
  posted: "Open questions are posted as sub-issues of one issue; answers come back as comments.",
  parent: "Issue",
  teamNotFound: "Linear has no team with this key, or your account cannot see it.",
  projectNotFound: "The team has no project with this name or link, or your account cannot see it.",
  keyInvalid: "Enter the key of a Linear team, for example ENG.",
  generic: "The Linear gap channel could not be saved. Try again.",
  errors: ERRORS,
  reasons: REASONS,
} as const;

/** The closed codes that mean the member who connected Linear has to connect again. */
export const LINEAR_RECONNECT: ReadonlySet<string> = new Set(["linear_not_connected", "linear_reconnect_required"]);
