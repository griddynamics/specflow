// docs/mission-control/integrations-api.md, DECISIONS D1-D4, D6, README "Integrations": the rules of the Integrations page, free of React.
// What a card shows from the API's answer (health words come from the API), who may disconnect, and what a failed call means.
import { atlassianToast } from "@/components/settings/jira-logic";
import { githubToast } from "@/components/settings/github-logic";
import { boxToast } from "../canvas/box-logic";
import { LINEAR_FAILURE, linearToast } from "./linear-logic";
import { dropboxToast } from "../canvas/dropbox-logic";
import { isDataError } from "@/lib/domain/errors";
import type { Integration, IntegrationApp } from "@/lib/domain/integrations";
import type { IntegrationState } from "@graphico/ui";
import { V2_COPY } from "../v2-copy";

export interface CardView {
  state: IntegrationState;
  /** The state in a word (the API's `health`), or none for an app nobody connected. */
  badge?: string;
  /** "Connected by <name>". */
  by?: string;
  actionLabel: string;
}

/** What a card shows. Grey and dashed until connected; ink while it works; ink outline when it is connected but nothing acts (D2). */
export function cardViewOf(i: Integration): CardView {
  if (!i.available) return { state: "unavailable", badge: V2_COPY.unavailableBadge, actionLabel: V2_COPY.unavailableAction };
  const bound = i.connectedBy !== null || i.workspace !== null;
  if (!bound) return { state: "off", actionLabel: V2_COPY.setUp };
  const by = i.connectedBy === null ? undefined : `${V2_COPY.connectedBy} ${i.connectedBy.name}`;
  return {
    state: i.health === "active" ? "on" : "stopped",
    badge: V2_COPY.health[i.health],
    by,
    actionLabel: V2_COPY.manage,
  };
}

/** Slack's channel is removed through its own web route; the other apps go through the integrations route. */
export const disconnectsThroughSlackRoute = (app: IntegrationApp): boolean => app === "slack";

/** The toast of `?github=`, `?atlassian=`, `?linear=`, `?box=` or `?dropbox=` on the way back from an app. */
export function returnToast(
  app: "atlassian" | "github" | "linear" | "box" | "dropbox",
  code: string,
): { title: string; tone: "success" | "danger" } {
  if (app === "linear") return linearToast(code);
  if (app === "box") return boxToast(code);
  if (app === "dropbox") return dropboxToast(code);
  return app === "github" ? githubToast(code) : atlassianToast(code);
}

/** Both apps come back with `connected` once the account is linked; GitHub also with `installed` after its repositories changed. */
export const isLinked = (code: string): boolean => code === "connected";

const FAILURE: Record<string, string> = {
  atlassian_not_connected: V2_COPY.notConnectedFirst,
  github_not_connected: V2_COPY.notConnectedFirst,
  box_not_connected: V2_COPY.notConnectedFirst,
  box_unavailable: "Box is not set up on this server.",
  dropbox_not_connected: V2_COPY.notConnectedFirst,
  dropbox_unavailable: "Dropbox is not set up on this server.",
  integration_workspace_mismatch: V2_COPY.workspaceMismatch,
  integration_already_connected: V2_COPY.alreadyConnected,
  integration_forbidden: V2_COPY.notAllowedToRemove,
  slack_unavailable: V2_COPY.slackNotOnServer,
  ...LINEAR_FAILURE,
};

/** What a failed connect or disconnect says; `gone` = the project is not the caller's any more (the page goes home). */
export function integrationFailure(e: unknown): { kind: "gone" } | { kind: "text"; text: string } {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  if (code === "project_not_found") return { kind: "gone" };
  return { kind: "text", text: Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed };
}
