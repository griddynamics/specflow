// docs/mission-control/integrations-api.md: the project's integrations. An integration binds one member's per-user connection (Atlassian,
// GitHub) or the Slack channel binding to the project; every use of the app in the project acts as that member. Screens reach it only
// through `IntegrationsPort`; the http adapter (`lib/data/http/integrations.ts`) is the one implementation.

/** The apps a project can connect, in the order the API lists them. */
export const INTEGRATION_APPS = ["atlassian", "github", "slack"] as const;
export type IntegrationApp = (typeof INTEGRATION_APPS)[number];

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
