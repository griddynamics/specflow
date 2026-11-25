// docs/mission-control/integrations-api.md, DECISIONS D1-D4, D6, README "Integrations": the rules of the Integrations page, free of React.
// What a card shows from the API's answer (health words come from the API), who may disconnect, and what a failed call means.
import { atlassianToast } from "@/components/settings/jira-logic";
import { githubToast } from "@/components/settings/github-logic";
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

/** Disconnect for the member who connected it or the owner (AC6). Slack is also removable by any member through its own route (D6). */
export function canDisconnect(i: Integration, meId: string | null, isOwner: boolean): boolean {
  if (i.connectedBy === null && i.workspace === null) return false;
  if (i.app === "slack") return true;
  return isOwner || (meId !== null && i.connectedBy?.userId === meId);
}

/** Slack's web route is member-level; the other apps go through the integrations route. */
export const disconnectsThroughSlackRoute = (app: IntegrationApp): boolean => app === "slack";

/** The toast of `?github=` or `?atlassian=` on the way back from an app. */
export function returnToast(app: "atlassian" | "github", code: string): { title: string; tone: "success" | "danger" } {
  return app === "github" ? githubToast(code) : atlassianToast(code);
}

/** Both apps come back with `connected` once the account is linked; GitHub also with `installed` after its repositories changed. */
export const isLinked = (code: string): boolean => code === "connected";

const FAILURE: Record<string, string> = {
  atlassian_not_connected: V2_COPY.notConnectedFirst,
  github_not_connected: V2_COPY.notConnectedFirst,
  integration_workspace_mismatch: V2_COPY.workspaceMismatch,
  integration_forbidden: V2_COPY.notAllowedToRemove,
  slack_unavailable: V2_COPY.slackNotOnServer,
};

/** What a failed connect or disconnect says; `gone` = the project is not the caller's any more (the page goes home). */
export function integrationFailure(e: unknown): { kind: "gone" } | { kind: "text"; text: string } {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  if (code === "project_not_found") return { kind: "gone" };
  return { kind: "text", text: Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed };
}
