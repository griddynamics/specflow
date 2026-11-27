// docs/notion/plan.md, docs/mission-control/integrations-api.md: a project's plan exported to a Notion database (stories and tasks as rows).
// Screens reach it only through `NotionPlanPort`.

export type NotionSyncStatus = "idle" | "running" | "succeeded" | "partial" | "failed" | "skipped";

/** What the last pass did, per row (`counts` is null until a pass finished). `deferred` rows are left for the next pass (a pass writes at most 120 rows). */
export interface NotionPlanCounts {
  created: number;
  updated: number;
  commented: number;
  removed: number;
  unchanged: number;
  failed: number;
  deferred: number;
}

export interface NotionPlanSync {
  status: NotionSyncStatus;
  queued: boolean;
  finishedAt: string | null;
  errorCode: string | null;
  counts: NotionPlanCounts | null;
}

/** `GET` and `PUT /projects/{pid}/notion-plan`. `available` false = Notion is off on the server (everything else is empty); `databaseUrl` is null until the first pass made the database. */
export interface NotionPlan {
  available: boolean;
  parentPageId: string | null;
  parentPageTitle: string | null;
  parentPageUrl: string | null;
  databaseUrl: string | null;
  enabled: boolean;
  /** The Notion workspace the export acts in. */
  connectedByName: string | null;
  connectedByMe: boolean;
  sync: NotionPlanSync | null;
}

/** A failed check is an answer, not an error: `reason` is a closed `notion_*` code. */
export type NotionPlanAccess = { result: "ok"; parentPageTitle: string } | { result: "failed"; reason: string };

export interface NotionPlanPort {
  get(projectId: string): Promise<NotionPlan>;
  /** Any member: turning it on (or another parent page) starts a pass in the background. `link` is the link of the parent page. */
  save(projectId: string, input: { link: string; enabled: boolean }): Promise<NotionPlan>;
  /** Any member: runs as the member Notion is connected by. */
  checkAccess(projectId: string, link: string): Promise<NotionPlanAccess>;
}
