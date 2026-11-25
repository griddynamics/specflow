// plans/LINEAR/LINEAR-PLAN.md (stream S3): a project's plan exported to a Linear team (stories as issues, tasks as sub-issues of one Linear project).
// Screens reach it only through `LinearPlanPort`.

export type LinearPlanSyncStatus = "idle" | "running" | "succeeded" | "partial" | "failed" | "skipped";

/** What the last pass did, per item (`counts` is null until a pass finished). */
export interface LinearPlanCounts {
  created: number;
  updated: number;
  commented: number;
  removed: number;
  relationsAdded: number;
  estimateRefused: number;
  unchanged: number;
  failed: number;
}

export interface LinearPlanSync {
  status: LinearPlanSyncStatus;
  queued: boolean;
  finishedAt: string | null;
  errorCode: string | null;
  counts: LinearPlanCounts | null;
}

/** `GET` and `PUT /projects/{pid}/linear-plan`. `available` false = Linear is off on the server (everything else is empty). */
export interface LinearPlan {
  available: boolean;
  enabled: boolean;
  teamId: string | null;
  teamKey: string | null;
  linearProjectId: string | null;
  linearProjectName: string | null;
  linearProjectUrl: string | null;
  /** True when Graphico made the Linear project (it is named after the Graphico project). */
  createdByGraphico: boolean;
  /** The Linear workspace the export acts in (the member Linear is connected by). */
  workspace: string | null;
  connectedByMe: boolean;
  sync: LinearPlanSync | null;
}

export interface LinearTeam {
  id: string;
  key: string;
  name: string;
}

export interface LinearTeams {
  workspace: string;
  teams: LinearTeam[];
}

/** A Linear project of a team. */
export interface LinearProjectChoice {
  id: string;
  name: string;
  url: string;
}

/** A team and one of its Linear projects, or `projectId` null to let Graphico create the project (named after this project). */
export interface LinearPlanTarget {
  teamId: string;
  projectId: string | null;
}

export type LinearPlanField = "team" | "project";

/** A failed check is an answer, not an error: `field` names the input to fix (null = none). */
export type LinearPlanAccess =
  { result: "ok"; teamKey: string; projectName: string | null } | { result: "failed"; reason: string; field: LinearPlanField | null };

export interface LinearPlanPort {
  get(projectId: string): Promise<LinearPlan>;
  /** The teams of the member Linear is connected by. */
  teams(projectId: string): Promise<LinearTeams>;
  /** The Linear projects of a team. */
  projects(projectId: string, teamId: string): Promise<LinearProjectChoice[]>;
  /** Any member: turning it on (or another team or project) starts a pass in the background. */
  save(projectId: string, input: LinearPlanTarget & { enabled: boolean }): Promise<LinearPlan>;
  /** Any member: runs as the member Linear is connected by. */
  checkAccess(projectId: string, target: LinearPlanTarget): Promise<LinearPlanAccess>;
}
