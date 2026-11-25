// docs/mission-control/integrations-api.md: the project's integrations. An integration binds one member's per-user connection (Atlassian,
// GitHub) or the Slack channel binding to the project; every use of the app in the project acts as that member. Screens reach it only
// through `IntegrationsPort`; the http adapter (`lib/data/http/integrations.ts`) is the one implementation.

/** The apps a project can connect, in the order the API lists them. */
export const INTEGRATION_APPS = ["atlassian", "github", "slack"] as const;
export type IntegrationApp = (typeof INTEGRATION_APPS)[number];

/**
 * What a card of the Integrations page can open: an app of the API, or MCP. MCP is only a card (the deployment's address and the commands to
 * connect a coding agent); the project has no connection to it, so it is not an `IntegrationApp` and the API types stay as they are.
 */
export type IntegrationCardId = IntegrationApp | "mcp";

/** The apps `PUT` binds from the caller's own connection; Slack is bound by its command in the channel. */
export type BindableApp = Exclude<IntegrationApp, "slack">;

/** `active`: the bound member's connection works (Slack: a channel is bound); `reconnect_required`: it expired; `none`: nothing acts. */
export type IntegrationHealth = "active" | "reconnect_required" | "none";

export interface Integration {
  app: IntegrationApp;
  /** The server runs the app. */
  available: boolean;
  connectedBy: { userId: string; name: string } | null;
  workspace: { id: string; name: string } | null;
  connectedAt: string | null;
  health: IntegrationHealth;
}

export interface IntegrationsPort {
  /** Always the three apps, in order. */
  list(projectId: string): Promise<Integration[]>;
  /** Binds the caller's own connection to the project (any member). */
  connect(projectId: string, app: BindableApp): Promise<Integration>;
  /** The member who connected it or the owner. */
  disconnect(projectId: string, app: IntegrationApp): Promise<void>;
}

/**
 * Disconnect is for the member who connected the app or the owner (AC6), Slack included. A project with nothing connected has nothing to remove.
 * The Slack channel bound before its integration row existed shows no connector: the card is off, and the Slack route lets any member remove it.
 */
export function canDisconnect(i: Integration, meId: string | null, isOwner: boolean): boolean {
  if (i.connectedBy === null && i.workspace === null) return false;
  return isOwner || (meId !== null && i.connectedBy?.userId === meId);
}
