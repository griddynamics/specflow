// docs/jira-sync/ui.md#plan-line: the texts of the Plan's Jira sync line. The step texts and the count come from the Settings copy.
export const JIRA_LINE_COPY = {
  label: "Jira sync",
  queued: "Jira sync queued",
  upToDate: "Jira is up to date",
  epic: "Epic",
  partial: "Jira sync finished with errors.",
  failed: "Jira sync failed.",
  reconnect: "Jira sync failed: an owner must connect Atlassian again.", // `jira_not_connected`, `jira_reconnect_required`
  openSettings: "Open Jira settings",
} as const;
