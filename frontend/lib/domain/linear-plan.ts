// plans/LINEAR/LINEAR-PLAN.md: a project's plan exported to a Linear team (stories as issues, tasks as sub-issues). FOUNDATION STUB: stream S3
// extends this file (teams, projects, save, access check) and its adapter. Screens reach it only through `LinearPlanPort`.

export type LinearPlanSyncStatus = "idle" | "running" | "succeeded" | "partial" | "failed" | "skipped";

export interface LinearPlanSync {
  status: LinearPlanSyncStatus;
  queued: boolean;
  finishedAt: string | null;
  errorCode: string | null;
}

/** `GET /projects/{pid}/linear-plan`. `available` false = Linear is off on the server (everything else is empty). */
export interface LinearPlan {
  available: boolean;
  enabled: boolean;
  teamId: string | null;
  teamKey: string | null;
  linearProjectId: string | null;
  linearProjectName: string | null;
  linearProjectUrl: string | null;
  sync: LinearPlanSync | null;
}

export interface LinearPlanPort {
  get(projectId: string): Promise<LinearPlan>;
}
