// The words and rules of the Dropbox link source (docs/dropbox/source.md), free of React: what a failed check or add says (the closed codes of the API), what a
// node says when its import stopped, and the toast of `?dropbox=` on the way back from Dropbox. A passed check and its file list read as a Box link's
// (`box-logic`: `boxCheckText`, `selectionText`, `folderHint`, `idOfFile`).
import { isDataError } from "@/lib/domain/errors";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";

/** What a Dropbox node says when the API reports its import failed (`no_access`, or any other closed reason). */
export const DROPBOX_WORDS = {
  noAccessMeta: "Cannot read it",
  noAccess: "The Dropbox account that connected it cannot read this folder or file now. Dropbox needs a reconnect in Integrations.",
  failedMeta: "Import failed",
  failed: "The import failed. Add the link again.",
} as const;

const FAILURE: Record<string, string> = {
  dropbox_not_connected: "Connect Dropbox in Integrations first.",
  dropbox_reconnect_required: "The Dropbox connection needs a reconnect in Integrations.",
  dropbox_unavailable: "Dropbox is not set up on this server.",
  dropbox_unreachable: "Dropbox did not answer. Try again in a minute.",
  dropbox_auth_failed: "Dropbox refused the request of this server. Ask the person who runs Graphico.",
  integration_workspace_mismatch: V2_COPY.workspaceMismatch,
  dropbox_link_invalid:
    "This is not a link to a Dropbox folder or file, or it has a password. Paste the address of one folder or file, not the whole Dropbox.",
  dropbox_no_access: "The Dropbox account connected to this project cannot open this folder or file. Add it to that Dropbox first.",
  source_not_found: "The source was removed. Add the link again.",
};

/** What a failed check or add says, from the API's closed code; anything else is the general failure. */
export function dropboxFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

/** Where a Dropbox node opens: the `https://www.dropbox.com/home<path>` link the API keeps, only when it is an https address on www.dropbox.com (Dropbox checks the viewer's access on open). */
export function dropboxOpen(link: string | null): { href: string; label: string } | undefined {
  if (link === null) return undefined;
  try {
    const url = new URL(link);
    const onDropbox = url.hostname === "www.dropbox.com";
    return url.protocol === "https:" && onDropbox ? { href: url.href, label: CANVAS_COPY.openDropbox } : undefined;
  } catch {
    return undefined;
  }
}

const TOAST: Record<string, string> = {
  dropbox_consent_denied: "Dropbox was not connected: access was not allowed.",
  dropbox_state_invalid: "The Dropbox sign-in expired. Try again.",
  dropbox_code_invalid: "Dropbox did not accept the sign-in. Try again.",
  dropbox_unreachable: "Dropbox did not answer. Try again in a minute.",
  dropbox_auth_failed: "Dropbox refused the request of this server. Ask the person who runs Graphico.",
  dropbox_unavailable: "Dropbox is not set up on this server.",
};

/** The toast of `?dropbox=<code>` on the way back from Dropbox. */
export function dropboxToast(code: string): { title: string; tone: "success" | "danger" } {
  if (code === "connected") return { title: "Dropbox connected", tone: "success" };
  return { title: Object.hasOwn(TOAST, code) ? TOAST[code] : "Dropbox could not be connected. Try again.", tone: "danger" };
}
