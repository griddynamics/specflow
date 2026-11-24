// docs/slack/gap-answering.md "The channel flow": the Slack channel of a project (Settings, Slack tab). The owner connects it from inside
// Slack (`/specflow connect <project id>`); its open questions are posted there, and a reply in a question's thread answers it.

/** The channel connected to the project. */
export interface SlackChannel {
  id: string;
  name: string;
  private: boolean;
}

/** `available`: the server runs the Slack app; `channel` the connected one (no other channel is ever listed); `editable`: the caller is the owner. */
export interface SlackSettings {
  available: boolean;
  channel: SlackChannel | null;
  editable: boolean;
}

export interface SlackPort {
  get(projectId: string): Promise<SlackSettings>;
  /** Owner only: disconnects the channel (the posted messages stay in Slack). */
  unbind(projectId: string): Promise<void>;
}

/** The command the owner runs in the Slack channel to connect it to the project. */
export const slackConnectCommand = (projectId: string) => `/specflow connect ${projectId}`;
