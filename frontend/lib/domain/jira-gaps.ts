// docs/jira-sync/gaps.md, docs/mission-control/integrations-api.md "Jira gap channel": a project's open gap questions posted to a Jira project as
// sub-tasks of one Story, the answers read back from their comments. Screens reach it only through `JiraGapsPort`.
import type { JiraAccess } from "./jira";

/** `one`: a sub-task per question; `area`: a sub-task per work area, its questions numbered. */
export type JiraGapGrouping = "one" | "area";

/**
 * `GET` and `PUT /projects/{pid}/jira-gaps`. `available` = the server has Atlassian OAuth and gap runs; everything else is empty when it is
 * not. `errorCode` is the closed Jira code the last pass or poll ended on. `pollSeconds` is how often the answers are read.
 */
export interface JiraGaps {
  available: boolean;
  jiraProject: string | null;
  grouping: JiraGapGrouping | null;
  enabled: boolean;
  storyKey: string | null;
  storyUrl: string | null;
  posted: number;
  answered: number;
  lastPolledAt: string | null;
  errorCode: string | null;
  pollSeconds: number;
}

export interface JiraGapsTarget {
  jiraProject: string;
  grouping: JiraGapGrouping;
}

export interface JiraGapsPort {
  get(projectId: string): Promise<JiraGaps>;
  /** Any member: turning it on (or another Jira project) posts the open questions in the background. */
  save(projectId: string, input: JiraGapsTarget & { enabled: boolean }): Promise<JiraGaps>;
  /** Any member: runs as the member Atlassian is connected by. A failed check is an answer. */
  checkAccess(projectId: string, jiraProject: string): Promise<JiraAccess>;
}
