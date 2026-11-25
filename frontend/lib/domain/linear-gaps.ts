// plans/LINEAR/LINEAR-PLAN.md: a project's open gap questions posted to Linear as sub-issues of one issue, the answers read back from their
// comments (webhook). FOUNDATION STUB: stream S5 extends this file (save, access check) and its adapter. Screens reach it only through
// `LinearGapsPort`.

/** `one`: an issue per question; `area`: an issue per work area, its questions numbered. */
export type LinearGapGrouping = "one" | "area";

/** `GET /projects/{pid}/linear-gaps`. `available` false = Linear (or gap runs) is off on the server (everything else is empty). */
export interface LinearGaps {
  available: boolean;
  enabled: boolean;
  teamKey: string | null;
  grouping: LinearGapGrouping | null;
  parentIdentifier: string | null;
  parentUrl: string | null;
  posted: number;
  answered: number;
  errorCode: string | null;
}

export interface LinearGapsPort {
  get(projectId: string): Promise<LinearGaps>;
}
