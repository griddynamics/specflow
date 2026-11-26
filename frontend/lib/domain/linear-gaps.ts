// docs/linear/gaps.md: a project's open gap questions posted to Linear as sub-issues of one issue, the answers read back from their comments
// (signed webhook). Screens reach it only through `LinearGapsPort`.

/** `one`: a sub-issue per question; `area`: a sub-issue per work area, its questions numbered. */
export type LinearGapGrouping = "one" | "area";

/**
 * `GET` and `PUT /projects/{pid}/linear-gaps`. `available` false = Linear (or gap runs) is off on the server (everything else is empty).
 * `errorCode` is the closed Linear code the last post or read ended on.
 */
export interface LinearGaps {
  available: boolean;
  enabled: boolean;
  teamKey: string | null;
  linearProjectId: string | null;
  grouping: LinearGapGrouping | null;
  parentIdentifier: string | null;
  parentUrl: string | null;
  posted: number;
  answered: number;
  errorCode: string | null;
}

/** What a person types: the team key, the optional Linear project (its name or a link) and the grouping. */
export interface LinearGapsTarget {
  teamKey: string;
  linearProject: string | null;
  grouping: LinearGapGrouping;
}

export type LinearGapsAccessField = "team_key" | "linear_project";

/** The answer of the access check: a failed check is an answer, not an error. */
export type LinearGapsAccess =
  | { result: "ok"; teamName: string; linearProjectName: string | null }
  | { result: "failed"; reason: string; field: LinearGapsAccessField | null };

export interface LinearGapsPort {
  get(projectId: string): Promise<LinearGaps>;
  /** Any member: turning it on (or another team or project) posts the open questions in the background. */
  save(projectId: string, input: LinearGapsTarget & { enabled: boolean }): Promise<LinearGaps>;
  /** Any member: runs as the member Linear is connected by. */
  checkAccess(projectId: string, input: Pick<LinearGapsTarget, "teamKey" | "linearProject">): Promise<LinearGapsAccess>;
}
