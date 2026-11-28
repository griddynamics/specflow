// docs/jira-sync/ui.md#ports, api.md#shapes, oauth.md#routes: the Jira settings of a project, the access check and the caller's Atlassian
// connection. Screens reach them only through `JiraPort`; the http adapter (`lib/data/http/jira.ts`) is the one implementation.

export type JiraSyncState = "idle" | "running" | "succeeded" | "partial" | "failed" | "skipped";

export type JiraSyncStep = "issues" | "links" | "order";

/** How far a running pass is: the step it works on and the items done of the total (`done` may exceed `total`; views clamp). */
export interface JiraSyncProgress {
  step: JiraSyncStep;
  done: number;
  total: number;
}

/** The part of `JiraSyncStatus` the Settings tab and the Plan show. `progress` is set only while the pass runs and has started its work. */
export interface JiraSync {
  status: JiraSyncState;
  finishedAt: string | null;
  errorCode: string | null;
  progress: JiraSyncProgress | null;
}

/**
 * The acting account of a target in the `oauth` mode (docs/jira-sync/oauth.md#acting-account-of-a-target): whose Atlassian connection the
 * sync uses. `missing` = that connection is gone (`connectedByName` is then null).
 */
export interface JiraActing {
  connectedByName: string | null;
  connectedByMe: boolean;
  status: "active" | "expired" | "missing";
}

/**
 * `GET` and `PUT /projects/{pid}/jira`: `available` = the server has an Atlassian account, `editable` = the caller is the owner.
 * `connection` is null outside the `oauth` mode and when the project has no target.
 */
export interface JiraSettings {
  available: boolean;
  editable: boolean;
  siteUrl: string | null;
  jiraProject: string | null;
  epicKey: string | null;
  createdEpicKey: string | null;
  enabled: boolean;
  sync: JiraSync | null;
  connection: JiraActing | null;
}

/** The server's Jira mode: `oauth` = each user connects their own Atlassian account, `token` = one account, null = Jira is off. */
export type AtlassianMode = "oauth" | "token";

/** `GET /integrations/atlassian`: the caller's own Atlassian connection (docs/jira-sync/oauth.md#routes). */
export interface AtlassianConnection {
  mode: AtlassianMode | null;
  connected: boolean;
  status: "active" | "expired" | null;
  accountName: string | null;
  connectedAt: string | null;
  lastUsedAt: string | null;
}

export interface JiraTarget {
  jiraProject: string;
  epicKey: string | null;
}

export type JiraAccessField = "jira_project" | "epic_key";

/** A failed check is an answer, not an error: `field` names the input to fix (null = no input). */
export type JiraAccess =
  { result: "ok"; projectName: string; epicSummary: string | null } | { result: "failed"; reason: string; field: JiraAccessField | null };

export interface JiraPort {
  get(projectId: string): Promise<JiraSettings>;
  save(projectId: string, input: JiraTarget & { enabled: boolean }): Promise<JiraSettings>; // owner only
  checkAccess(projectId: string, target: JiraTarget): Promise<JiraAccess>; // owner only
  /** The caller's Atlassian connection (every mode). */
  connection(): Promise<AtlassianConnection>;
  /** The Atlassian consent URL; the browser returns to `returnTo` (a same-site path) through the callback route. */
  authorize(returnTo: string): Promise<string>;
}
