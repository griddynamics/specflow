// MCP-M10: the Guides snippets. Small pure functions from the deployment's values; no React, no config import (the server page reads
// `appConfig()` and passes plain values). Every text a COPY button copies is built here, from the inputs alone.

export interface SnippetInput {
  /** The MCP address (`MCP_PUBLIC_URL`). */
  mcpUrl: string;
  /** The OAuth client id (`MCP_CLIENT_ID`). */
  clientId: string;
  projectId: string;
  projectName: string;
  /** The web app's dev-login user (`LOCAL_TEST_USER_EMAIL`), used by the local variant only. */
  devEmail: string;
}

/** Which setup the page shows: production sign-in (`oidc`) or the local dev token (`dev`). */
export type GuideMode = "oidc" | "dev";

export const PRODUCTION_SERVER = "specflow";
export const LOCAL_SERVER = "specflow-local";
export const CALLBACK_PORT = 8765;
export const LOCAL_CHECKOUT_PLACEHOLDER = "<path to your SpecFlow checkout>";

export const serverName = (mode: GuideMode): string => (mode === "dev" ? LOCAL_SERVER : PRODUCTION_SERVER);

/** Quote a value for a POSIX shell inside single quotes. */
const shellQuote = (value: string): string => `'${value.replaceAll("'", "'\\''")}'`;

type Pick<K extends keyof SnippetInput> = { [P in K]: SnippetInput[P] };

/** Step 1, production: add the server for every repository on this computer. */
export function claudeAddCommand({ mcpUrl, clientId }: Pick<"mcpUrl" | "clientId">): string {
  return `claude mcp add --transport http --scope user --client-id ${clientId} --callback-port ${String(CALLBACK_PORT)} ${PRODUCTION_SERVER} ${mcpUrl}`;
}

/** The team file: commit it and everyone who opens the repository gets SpecFlow. Pretty JSON, two spaces. */
export function teamMcpJson({ mcpUrl, clientId }: Pick<"mcpUrl" | "clientId">): string {
  return JSON.stringify(
    { mcpServers: { [PRODUCTION_SERVER]: { type: "http", url: mcpUrl, oauth: { clientId, callbackPort: CALLBACK_PORT } } } },
    null,
    2,
  );
}

/** Step 2: the sign-in command in Claude Code. */
export const SIGN_IN_COMMAND = "/mcp";

/** Step 1, local development: the `headersHelper` entry at local scope (deployment.md, "Local endpoint"). */
export function localAddCommand({ mcpUrl, devEmail }: Pick<"mcpUrl" | "devEmail">): string {
  const entry = JSON.stringify({
    type: "http",
    url: mcpUrl,
    headersHelper: `cd ${LOCAL_CHECKOUT_PLACEHOLDER}/backend && uv run --quiet --env-file ../.env.local python examples/mcp_qa.py dev-token ${devEmail} --headers`,
  });
  return `claude mcp add-json --scope local ${LOCAL_SERVER} ${shellQuote(entry)}`;
}

/** Step 3: the prompt that binds a repository; with a project id it skips the list (Claude Code splits arguments at spaces). */
export function bindCommand(mode: GuideMode, projectId?: string): string {
  const base = `/mcp__${serverName(mode)}__select_project`;
  return projectId === undefined || projectId === "" ? base : `${base} ${projectId}`;
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
