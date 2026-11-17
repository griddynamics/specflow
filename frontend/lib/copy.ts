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
  listChanged: "The list changed. It is up to date now.", // toast: member_not_found, invite_not_found, cannot_remove_admin
  saveFailed: "Could not save. Try again.", // toast: admin_required, network, anything else
  claimsNotFreed: "Some story claims could not be freed. Free the rest in Plan > Work.", // toast after a removal with claims_release_failed
  nameEmpty: "Enter a project name.", // the Create Project name messages, shared with the Settings name field
  nameTooLong: "Use 200 characters or fewer.",
  // Screen captions:
  settingsSection: "Settings section", // the SegmentedControl label in the shell header
  settingsTabGeneral: "General",
  settingsProjectName: "Project name",
  settingsSave: "Save",
  collaboratorsHeading: "Collaborators",
  collaboratorEmail: "Email address",
  collaboratorAdd: "Add",
  badgeAdmin: "Admin",
  badgeInvited: "Invited",
  notSignedInYet: "Not signed in yet",
  leaveProject: "Leave project",
} as const;
