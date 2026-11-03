// MCP-M10: the Guides snippets. Small pure functions from the deployment's values; no React, no config import (the server page reads
// `appConfig()` and passes plain values). Every text a COPY button copies is built here, from the inputs alone.

export interface SnippetInput {
  /** The MCP address (`MCP_PUBLIC_URL`). */
  mcpUrl: string;
  /** The OAuth client id (`MCP_CLIENT_ID`). */
  clientId: string;
  projectId: string;
  projectName: string;
  /** The name the server is registered under in Claude Code (`MCP_SERVER_NAME`). */
  serverName: string;
}

export const CALLBACK_PORT = 8765;

type Pick<K extends keyof SnippetInput> = { [P in K]: SnippetInput[P] };

/** Step 1: add the server for every repository on this computer. */
export function claudeAddCommand({ mcpUrl, clientId, serverName }: Pick<"mcpUrl" | "clientId" | "serverName">): string {
  return `claude mcp add --transport http --scope user --client-id ${clientId} --callback-port ${String(CALLBACK_PORT)} ${serverName} ${mcpUrl}`;
}

/** The team file: commit it and everyone who opens the repository gets SpecFlow. Pretty JSON, two spaces. */
export function teamMcpJson({ mcpUrl, clientId, serverName }: Pick<"mcpUrl" | "clientId" | "serverName">): string {
  return JSON.stringify(
    { mcpServers: { [serverName]: { type: "http", url: mcpUrl, oauth: { clientId, callbackPort: CALLBACK_PORT } } } },
    null,
    2,
  );
}

/** Step 2: the sign-in command in Claude Code. */
export const SIGN_IN_COMMAND = "/mcp";

/** Step 3: the prompt that binds a repository; with a project id it skips the list (Claude Code splits arguments at spaces). */
export function bindCommand(serverName: string, projectId?: string): string {
  const base = `/mcp__${serverName}__select_project`;
  return projectId === undefined || projectId === "" ? base : `${base} ${projectId}`;
}

/** The project-name placeholder of the step 3 prompt, shown when the app does not know the active project. */
export const PROJECT_NAME_PLACEHOLDER = "<project name>";

/** Step 3: what to say to Claude Code to bind a repository. The name goes in verbatim inside double quotes (a chat prompt, not JSON). */
export function connectPrompt(projectName: string | null): string {
  const project = projectName === null || projectName === "" ? PROJECT_NAME_PLACEHOLDER : `"${projectName}"`;
  return `Connect this repository to the SpecFlow project ${project}.`;
}

/** Step 4: what to say to check the connection. */
export const CHECK_PROMPT = "Which SpecFlow projects can I work on?";

/** The binding file (`.specflow/config.json`), pretty JSON; JSON.stringify keeps a name with quotes or non-ASCII valid. */
export function bindingFileJson({ mcpUrl, projectId, projectName }: Pick<"mcpUrl" | "projectId" | "projectName">): string {
  return JSON.stringify({ version: 1, server: mcpUrl, project_id: projectId, project_name: projectName }, null, 2);
}

/** The two sample prompts of Plan › Develop. */
export const DEVELOP_OVERVIEW_PROMPT = "List the features of this project.";
export const DEVELOP_START_PROMPT = "Summarise each feature of this project in one line, then propose where to start in this repository.";
