// The words of the canvas, its plus dialog and its forms. Texts that exist elsewhere (the Drive reasons, the upload formats, the build
// states) are imported from their modules, not written again.
export const CANVAS_COPY = {
  canvasLabel: "Mission Control canvas",
  addIntro: {
    graph: "Graphico imports what you add, once, and builds one graph from it. A changed document is added again.",
    gap: "Graphico asks its questions where the people who can answer already talk, and reads the answers back.",
    plan: "The plan goes where the team tracks work, and is kept up to date there.",
  },
  addTitle: { graph: "Add a source", gap: "Add a gap channel", plan: "Add a plan destination" },
  zoneName: { graph: "Graph", gap: "Gap", plan: "Plan" },
  zoneNumber: { graph: "01", gap: "02", plan: "03" },
  nothingYet: "Nothing can be added to this stage yet.",
  others: "Connections for the other stages",
  forStage: "For",
  available: "Available",
  setUpFirst: "Set up first",
  setUpLine: "Connect {app} in Integrations first.",
  alwaysOn: "Always on: MCP for coding agents. Apps are connected in",
  integrationsLink: "Integrations",
  back: "Back",
  // Upload
  uploadTitle: "Add files",
  dropTitle: "Drop files here",
  chooseFiles: "Choose files",
  // Google Drive
  driveStepShare: "Share the folder with this account as a Viewer:",
  driveStepPaste: "Paste the folder link below and test it.",
  driveLink: "Google Drive folder link",
  driveCopy: "Copy",
  driveCopied: "Copied",
  driveAccount: "Drive account email",
  driveSaving: "Saving…",
  // MCP
  mcpEyebrow: "Added automatically",
  mcpTitle: "Connect a coding agent over MCP",
  mcpSay: {
    gap: "Coding agents see the open gaps of their work, answer the ones they can, and report new findings.",
    plan: "Coding agents claim a whole story, read its brief and report every task as they build it.",
  },
  mcpStepAdd: "Add the server in Claude Code:",
  mcpStepSignIn: "Sign in:",
  mcpStepBind: "Bind the repository to this project:",
  mcpTools: "Tools for this stage",
  mcpOther: "Cursor, Codex and other clients: add the same address as a remote MCP server.",
  done: "Done",
} as const;

export const MCP_TOOLS = {
  gap: ["list_gaps", "browse_gaps", "open_gap", "answer_gap", "add_finding"],
  plan: ["list_areas", "list_stories", "claim_story", "get_story_brief", "report_task", "complete_story"],
} as const;
