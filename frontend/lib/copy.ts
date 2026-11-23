// The text catalogue of the screens: user-facing captions and placeholders live here, once, so a screen takes its wording by key
// (and a translation replaces this one module). The wording is the app's English, unchanged.
export const COPY = {
  loadingFiles: "Loading files",
  loadingDocument: "Loading the document",
  loadingMissionControl: "Loading Mission Control",
  loadingProject: "Loading project",
  loadingEmail: "Loading…",
  loadingCaption: "Loading",
  searchConnectors: "Search connectors",
  reviewerComment: "Add context for the reviewer",
  projectName: "e.g. Payments platform",
  planCreateUnavailable: "Plan creation is not available yet.",
  driveFolderLink: "https://drive.google.com/drive/folders/…",
  // Settings > General and the Collaborators field of Create Project (docs/projects/ui.md#errors-shown). Errors:
  emailInvalid: "Enter a valid email address.", // `invalid_email` and the local format check
  emailDomainNotAllowed: "This email domain cannot sign in to SpecFlow.",
  alreadyCollaborator: "Already a collaborator.",
  alreadyInvited: "Already invited.",
  memberLimit: "This project has 50 people. Remove someone first.",
  listChanged: "The list changed. It is up to date now.", // toast: member_not_found, invite_not_found, cannot_remove_owner
  saveFailed: "Could not save. Try again.", // toast: owner_required, network, anything else
  claimsNotFreed: "Some story claims could not be freed. Free the rest in Plan > Work.", // toast after a removal with claims_release_failed
  nameEmpty: "Enter a project name.", // the Create Project name messages, shared with the Settings name field
  nameTooLong: "Use 200 characters or fewer.",
  // Settings > Jira (docs/jira-sync/ui.md#errors-shown): the texts of the access-check reasons and of the sync status codes.
  jiraAuthFailed: "Jira refused the Specflow account. An administrator checks the Atlassian token.", // `token` mode
  jiraProjectNotFound: "No Jira space with this key, or the account cannot see it.",
  jiraIssueTypesMissing: "This Jira space has no Epic, Story or Sub-task issue type.",
  jiraForbidden: "The Specflow account may not create, edit, link or rank issues in this space.",
  jiraEpicNotFound: "No issue with this key.",
  jiraEpicInvalid: "This issue is not an Epic of the Jira space.",
  jiraUnreachable: "Jira did not answer. Try again.", // `jira_unreachable` and `jira_unexpected`
  jiraKeyInvalid: "Use a Jira key such as ABC or ABC-123.", // `validation_failed`
  jiraUnavailable: "Jira is not connected to this Specflow server. An administrator connects an Atlassian account.",
  jiraNoPlan: "No plan yet.",
  jiraPlanApplying: "The plan was being updated.",
  jiraNotConfigured: "Sync is off.",
  jiraInterrupted: "Stopped by a restart.",
  jiraTargetChanged: "The Jira target changed during the sync.",
  jiraUnexpectedError: "Something went wrong.", // `unexpected_error` and any code not listed
  // Atlassian connections (docs/jira-sync/oauth.md): the access-check reasons and the sync codes of the `oauth` mode, and the
  // `?atlassian=` toasts of the consent callback.
  jiraNotConnected: "Connect your Atlassian account first.", // access check `jira_not_connected`, PUT 409
  jiraReconnectRequired: "Your Atlassian connection expired. Reconnect Atlassian.", // access check `jira_reconnect_required`
  jiraSyncNotConnected: "No Atlassian account to sync as. An owner connects Atlassian and saves the target.", // sync `jira_not_connected`
  jiraSyncReconnect: "The Atlassian connection this target syncs as expired. Its owner reconnects Atlassian.", // sync `jira_reconnect_required`
  jiraAuthFailedOAuth: "Atlassian refused the connection. Reconnect Atlassian in Settings → Jira.", // access check `jira_auth_failed`
  jiraSyncAuthFailedOAuth: "Atlassian refused the connection this target syncs as. Its owner reconnects Atlassian.", // sync `jira_auth_failed`
  atlassianConnected: "Atlassian connected",
  atlassianConsentDenied: "Atlassian access was not granted.",
  atlassianStateInvalid: "The Atlassian sign-in expired. Try again.",
  atlassianCodeInvalid: "Atlassian did not accept the sign-in. Try again.",
  atlassianSiteNotGranted: "Your Atlassian account has no access to this Jira site.",
  atlassianUnreachable: "Atlassian could not be reached. Try again later.",
  atlassianUnavailable: "Atlassian connections are not set up on this Specflow server.",
  atlassianUnexpected: "Could not connect Atlassian. Try again.",
  // Screen captions:
  // Settings > GitHub (plans/GITHUB-INTEGRATION/contract.md): the `?github=` toasts of the return routes, the API refusals and the
  // reasons a clone check failed.
  githubConnected: "GitHub connected",
  githubInstalled: "GitHub repositories updated",
  githubConsentDenied: "GitHub access was not granted.",
  githubStateInvalid: "The GitHub sign-in expired. Try again.",
  githubCodeInvalid: "GitHub did not accept the sign-in. Try again.",
  githubUnreachable: "GitHub could not be reached. Try again later.",
  githubUnavailable: "GitHub is not set up on this Specflow server. An administrator adds the Specflow GitHub App.",
  githubUnexpected: "Could not connect GitHub. Try again.",
  githubNotConnected: "Connect your GitHub account first.",
  githubReconnectRequired: "Your GitHub connection expired. Reconnect GitHub.",
  githubRepoNotAccessible: "Your GitHub account cannot read this repository through the Specflow app any more.",
  githubRepoTooLarge: "This repository is larger than 1 GB. Specflow cannot copy it.",
  githubAuthFailed: "GitHub refused the request. Reconnect GitHub; if it happens again, an administrator checks the Specflow GitHub App.",
  githubRepositoryExists: "This repository is already in the project.",
  githubActionFailed: "Something went wrong. Try again.",
  // Why the last clone check failed (`failure_code`).
  cloneNotConnected: "Nobody can clone it: the person who added it is no longer connected to GitHub. An owner adds it again.",
  cloneReconnect: "The GitHub connection of the person who added it expired. They reconnect GitHub.",
  cloneNotAccessible: "GitHub refused access. The repository was removed from the Specflow app or the person who added it lost access.",
  cloneTooLarge: "The repository is larger than 1 GB.",
  cloneAuthFailed:
    "GitHub refused the clone. The person who added it reconnects GitHub, or an administrator checks the Specflow GitHub App.",
  cloneUnreachable: "GitHub did not answer. Clone again later.",
  cloneFailed: "Git could not clone the repository.",
  cloneTimeout: "The clone took longer than 10 minutes.",
  cloneStorageFailed: "The copy could not be stored. Clone again later.",
  cloneUnexpected: "The clone check failed.",
  settingsSection: "Settings section", // the SegmentedControl label in the shell header
  settingsTabGeneral: "General",
  settingsTabJira: "Jira",
  settingsTabSlack: "Slack",
  settingsTabGitHub: "GitHub",
  settingsProjectName: "Project name",
  settingsSave: "Save",
  collaboratorsHeading: "Collaborators",
  collaboratorEmail: "Email address",
  collaboratorAdd: "Add",
  badgeOwner: "Owner",
  badgeInvited: "Invited",
  notSignedInYet: "Not signed in yet",
  leaveProject: "Leave project",
} as const;
