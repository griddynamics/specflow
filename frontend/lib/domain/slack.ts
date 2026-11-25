// docs/slack/gap-answering.md "The channel flow": the Slack channel of a project (Settings, Slack tab). A member connects it from inside
// Slack (`/specflow connect <project id>`); its open questions are posted there, and a reply in a question's thread answers it.

/** The channel connected to the project. */
export interface SlackChannel {
  id: string;
  name: string;
  private: boolean;
}

/** The answer of `POST /projects/{pid}/slack/test`: the channel the welcome message went to. */
export interface SlackTestResult {
  posted: true;
  channel: SlackChannel;
}

/** `available`: the server runs the Slack app; `channel` the connected one (no other channel is ever listed); `editable`: the caller may change it (any member). */
export interface SlackSettings {
  available: boolean;
  channel: SlackChannel | null;
  editable: boolean;
}

export interface SlackPort {
  get(projectId: string): Promise<SlackSettings>;
  /** Any member (D6): disconnects the channel (the posted messages stay in Slack). */
  unbind(projectId: string): Promise<void>;
  /** Any member: posts the welcome message in the bound channel, so the person sees it arrive. */
  test(projectId: string): Promise<SlackTestResult>;
}

/** The command a member runs in the Slack channel to connect it to the project. */
export const slackConnectCommand = (projectId: string) => `/specflow connect ${projectId}`;
