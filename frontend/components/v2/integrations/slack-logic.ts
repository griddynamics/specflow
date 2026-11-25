// docs/mission-control/integrations-api.md "Slack test message": what the Slack set-up says, free of React. The channel is bound from
// inside Slack, so the dialog waits for it; Test connection posts the welcome message.
import { isDataError } from "@/lib/domain/errors";
import { V2_COPY } from "../v2-copy";

const TEST_FAILURE: Record<string, string> = {
  slack_not_connected: "No channel is connected yet. Run the command in the channel first.",
  slack_not_in_channel: "The app is not in the channel any more. Invite it again.",
  slack_unreachable: "Slack could not be reached. Try again.",
  slack_unavailable: V2_COPY.slackNotOnServer,
};

/** The line under Test connection after a failed post. */
export function slackTestFailure(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return Object.hasOwn(TEST_FAILURE, code) ? TEST_FAILURE[code] : V2_COPY.actionFailed;
}

/** How often the dialog asks whether the channel is bound yet. */
export const SLACK_POLL_MS = 3000;

/** `#name`, with "(private)" after a private channel. */
export const channelLabel = (c: { name: string; private: boolean }): string => `#${c.name}${c.private ? " (private)" : ""}`;

const LANDING: Record<string, { title: string; tone: "success" | "danger" }> = {
  linked: { title: "Slack linked to your account. Run the connect command in the channel again.", tone: "success" },
  slack_link_invalid: { title: "The Slack link expired. Run /specflow link in Slack again.", tone: "danger" },
  slack_unavailable: { title: V2_COPY.slackNotOnServer, tone: "danger" },
};

/** The one toast of `/?slack=<code>`, where the link route of `/specflow link` sends the browser. Any other value is a failure. */
export function slackLandingToast(code: string): { title: string; tone: "success" | "danger" } {
  return Object.hasOwn(LANDING, code) ? LANDING[code] : { title: "Could not link Slack. Run /specflow link again.", tone: "danger" };
}
