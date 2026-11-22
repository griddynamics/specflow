// docs/jira-sync/ui.md#ports, api.md#shapes: the Jira settings of a project and the access check. Screens reach them only through
// `JiraPort`; the http adapter (`lib/data/http/jira.ts`) is the one implementation.

export type JiraSyncState = "idle" | "running" | "succeeded" | "partial" | "failed" | "skipped";

/** The part of `JiraSyncStatus` the Settings tab shows. */
export interface JiraSync {
  status: JiraSyncState;
  finishedAt: string | null;
  errorCode: string | null;
}

/** `GET` and `PUT /projects/{pid}/jira`: `available` = the server has an Atlassian account, `editable` = the caller is the owner. */
export interface JiraSettings {
  available: boolean;
  editable: boolean;
  siteUrl: string | null;
  jiraProject: string | null;
  epicKey: string | null;
  createdEpicKey: string | null;
  enabled: boolean;
  sync: JiraSync | null;
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
}
