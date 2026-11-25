// The Notion card's rules, free of React: what the set-up dialog offers from the project's binding and the caller's own connection, and the
// words of the `?notion=` return. Words the header, the nav and the card already show (the name, the health badge) are not repeated here.
import type { Integration } from "@/lib/domain/integrations";
import type { NotionConnection } from "@/lib/domain/notion-connection";

export const NOTION_WORDS = {
  addPages: "Add pages in Notion",
  forget: "Forget my Notion account",
  forgotten: "Notion account removed",
  unavailable: "Notion is not available on this server.",
  loadFailed: "Your Notion connection could not be read. Close this and try again.",
  returnToast: {
    notion_consent_denied: "Notion access was not approved. Connect again when you are ready.",
    notion_state_invalid: "The Notion sign-in expired. Connect again.",
    notion_code_invalid: "Notion did not accept the approval. Connect again.",
    notion_unreachable: "Notion could not be reached. Try again.",
    notion_auth_failed: "Notion refused the access. Connect again.",
    notion_unavailable: "Notion is not available on this server.",
  } as Record<string, string>,
  unexpected: "Notion could not be connected. Try again.",
  reconnectFirst: "Your Notion access ended. Reconnect Notion first.",
} as const;

/** The toast of `?notion=<code>` for a refusal (`connected` is the page's own bind and toast). */
export const notionReturnWord = (code: string): string =>
  Object.hasOwn(NOTION_WORDS.returnToast, code) ? NOTION_WORDS.returnToast[code] : NOTION_WORDS.unexpected;

export interface NotionSetupView {
  /** `loading` until the caller's connection is read; `unavailable` when the server has Notion off. */
  state: "loading" | "unavailable" | "ready";
  /** The primary button: absent while the project's Notion works. */
  primary: "connect" | "reconnect" | null;
  /** The how-to shows until the project's Notion works. */
  howTo: boolean;
  /** "Add pages in Notion": the member who holds the project's binding connects again to tick more pages. */
  addPages: boolean;
  /** "Forget my Notion account": the caller has a connection of their own. */
  forget: boolean;
  /** The button only binds the caller's active connection to the project (no trip to Notion). */
  bindsOnly: boolean;
}

export function notionSetupViewOf(
  i: Pick<Integration, "connectedBy" | "workspace" | "health">,
  canDisconnect: boolean,
  mine: NotionConnection | null,
): NotionSetupView {
  const bound = i.connectedBy !== null || i.workspace !== null;
  const working = bound && i.health === "active";
  if (mine !== null && !mine.enabled)
    return { state: "unavailable", primary: null, howTo: false, addPages: false, forget: false, bindsOnly: false };
  const active = mine?.status === "active";
  const stopped = (bound && !working) || mine?.status === "expired";
  return {
    state: mine === null ? "loading" : "ready",
    primary: working ? null : stopped ? "reconnect" : "connect",
    howTo: !working,
    addPages: working && canDisconnect && active,
    forget: mine?.connected === true,
    bindsOnly: !working && active,
  };
}
