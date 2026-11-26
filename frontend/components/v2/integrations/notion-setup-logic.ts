// The Notion card's rules, free of React: what the set-up dialog offers from the project's binding and the caller's own connection, and the
// words of the `?notion=` return. Words the header, the nav and the card already show (the name, the health badge) are not repeated here.
import type { Integration } from "@/lib/domain/integrations";
import type { NotionConnection } from "@/lib/domain/notion-connection";

const RETURN_TOAST: Record<string, string> = {
  notion_consent_denied: "Notion access was not approved. Connect again when you are ready.",
  notion_state_invalid: "The Notion sign-in expired. Connect again.",
  notion_code_invalid: "Notion did not accept the approval. Connect again.",
  notion_unreachable: "Notion could not be reached. Try again.",
  notion_auth_failed: "Notion refused the access. Connect again.",
  notion_unavailable: "Notion is not available on this server.",
};

export const NOTION_WORDS = {
  addPages: "Add pages in Notion",
  forget: "Forget my Notion account",
  forgetTitle: "Forget your Notion account?",
  forgetBody:
    "Your Notion account is removed from Graphico. Every project that uses this account stops reading and writing Notion until someone reconnects.",
  forgotten: "Notion account removed. Projects that used it need reconnecting.",
  tryAgain: "Try again",
  unavailable: "Notion is not available on this server.",
  loadFailed: "Your Notion connection could not be read.",
  returnToast: RETURN_TOAST,
  unexpected: "Notion could not be connected. Try again.",
  reconnectFirst: "Notion access ended. Reconnect Notion first.",
} as const;

/** The toast of `?notion=<code>` for a refusal (`connected` is the page's own bind and toast). */
export const notionReturnWord = (code: string): string =>
  Object.hasOwn(NOTION_WORDS.returnToast, code) ? NOTION_WORDS.returnToast[code] : NOTION_WORDS.unexpected;

export interface NotionSetupView {
  /** `loading` until the caller's connection is read; `unavailable` when the server has Notion off. */
  state: "loading" | "failed" | "unavailable" | "ready";
  /** The primary button: absent while the project's Notion works and the caller's own connection is fine. */
  primary: "connect" | "reconnect" | null;
  /** The how-to shows until the project's Notion works. */
  howTo: boolean;
  /** "Add pages in Notion": the member who holds the project's binding connects again to tick more pages. */
  addPages: boolean;
  /** The connection could not be read: a notice and Try again, not a button stuck waiting. */
  retry: boolean;
  /** The project is bound but stopped: the notice above Reconnect. */
  stoppedNotice: boolean;
  /** "Forget my Notion account": the caller has a connection of their own. */
  forget: boolean;
  /** The button only binds the caller's active connection to the project (no trip to Notion). */
  bindsOnly: boolean;
}

const NOTHING = {
  primary: null,
  howTo: false,
  retry: false,
  stoppedNotice: false,
  addPages: false,
  forget: false,
  bindsOnly: false,
} as const;

export function notionSetupViewOf(
  i: Pick<Integration, "connectedBy" | "workspace" | "health">,
  canDisconnect: boolean,
  mine: NotionConnection | "failed" | null,
): NotionSetupView {
  const bound = i.connectedBy !== null || i.workspace !== null;
  const working = bound && i.health === "active";
  const read = mine === null || mine === "failed" ? null : mine;
  if (read !== null && !read.enabled) {
    return { ...NOTHING, state: "unavailable" };
  }
  let state: NotionSetupView["state"] = "ready";
  if (mine === null) state = "loading";
  else if (mine === "failed") state = "failed";
  let primary: NotionSetupView["primary"] = "connect";
  // The caller's own expired connection needs Reconnect even when the project's binding works (it is the account that acts).
  if (read?.status === "expired" || (bound && !working)) primary = "reconnect";
  else if (working) primary = null;
  return {
    state,
    primary,
    howTo: !working,
    retry: mine === "failed",
    stoppedNotice: bound && !working,
    addPages: working && canDisconnect && read?.connected === true,
    forget: read?.connected === true,
    bindsOnly: !working && read?.status === "active",
  };
}
