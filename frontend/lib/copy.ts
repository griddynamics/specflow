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
  jiraAuthFailed: "Jira refused the Specflow account. An administrator checks the Atlassian token.",
  jiraProjectNotFound: "No Jira project with this key, or the account cannot see it.",
  jiraIssueTypesMissing: "This Jira project has no Epic, Story or Sub-task issue type.",
  jiraForbidden: "The Specflow account may not create, edit, link or rank issues in this project.",
  jiraEpicNotFound: "No issue with this key.",
  jiraEpicInvalid: "This issue is not an Epic of the Jira project.",
  jiraUnreachable: "Jira did not answer. Try again.", // `jira_unreachable` and `jira_unexpected`
  jiraKeyInvalid: "Use a Jira key such as ABC or ABC-123.", // `validation_failed`
  jiraUnavailable: "Jira is not connected to this Specflow server. An administrator connects an Atlassian account.",
  jiraNoPlan: "No plan yet.",
  jiraPlanApplying: "The plan was being updated.",
  jiraNotConfigured: "Sync is off.",
  jiraInterrupted: "Stopped by a restart.",
  jiraTargetChanged: "The Jira target changed during the sync.",
  jiraUnexpectedError: "Something went wrong.", // `unexpected_error` and any code not listed
  // Screen captions:
  settingsSection: "Settings section", // the SegmentedControl label in the shell header
  settingsTabGeneral: "General",
  settingsTabJira: "Jira",
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
