CREATE UNIQUE INDEX IF NOT EXISTS ux_jira_issues_jira_key
ON jira_issues (project_id, jira_key)
WHERE jira_key IS NOT NULL
