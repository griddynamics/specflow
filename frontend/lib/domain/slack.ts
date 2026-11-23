// docs/slack/gap-answering.md "The channel flow": the Slack channel of a project (Settings, Slack tab). Its open questions are posted
// there, and a reply in a question's thread answers it.

/** A channel the SpecFlow Slack app is in. */
export interface SlackChannel {
  id: string;
  name: string;
  private: boolean;
}

/** `available`: the server runs the Slack app; `channel` the bound one; `channels` the ones to choose from; `editable`: the caller is the owner. */
export interface SlackSettings {
  available: boolean;
  channel: SlackChannel | null;
  channels: SlackChannel[];
  editable: boolean;
}

export interface SlackPort {
  get(projectId: string): Promise<SlackSettings>;
  /** Owner only: binds the channel and posts the project's open questions there. */
  bind(projectId: string, channelId: string): Promise<SlackSettings>;
  /** Owner only: no channel any more (the posted messages stay in Slack). */
  unbind(projectId: string): Promise<void>;
}
