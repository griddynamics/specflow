// The words and rules of the Box link source (docs/box/source.md), free of React: what a failed check or add says (the closed codes of the API), what a
// passed check says, what a node says when its import stopped, and the toast of `?box=` on the way back from Box.
import { isDataError } from "@/lib/domain/errors";
import type { BoxCheck } from "@/lib/domain/sources";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";

/** What a Box node says when the API reports its import failed (`no_access`, or any other closed reason). */
export const BOX_WORDS = {
  noAccessMeta: "Cannot read it",
  noAccess: "The Box account that connected it cannot read this folder or file now. Another member reconnects Box.",
  failedMeta: "Import failed",
  failed: "The import failed. Add the link again.",
} as const;

const FAILURE: Record<string, string> = {
  box_not_connected: "Connect Box in Integrations first.",
  box_reconnect_required: "The Box connection needs a reconnect in Integrations.",
  box_unavailable: "Box is not set up on this server.",
  box_unreachable: "Box did not answer. Try again in a minute.",
  box_auth_failed: "Box refused the request of this server. Ask the person who runs Graphico.",
  box_link_invalid: "This is not a link to a Box folder, file or shared link. Paste the address of one folder or file.",
  box_link_password: "This shared link has a password. Graphico cannot read it. Paste a link without a password.",
  box_no_access: "The Box account connected to this project cannot open this folder or file.",
  source_not_found: "The source was removed. Add the link again.",
};

/** What a failed check or add says, from the API's closed code; anything else is the general failure. */
export function boxFailureText(e: unknown): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

/** Where a Box node opens: the canonical link the API keeps, only when it is an https address on Box (Box checks the viewer's access on open). */
export function boxOpen(link: string | null): { href: string; label: string } | undefined {
  if (link === null) return undefined;
  try {
    const url = new URL(link);
    const onBox = url.hostname === "app.box.com" || url.hostname.endsWith(".app.box.com");
    return url.protocol === "https:" && onBox ? { href: url.href, label: CANVAS_COPY.openBox } : undefined;
  } catch {
    return undefined;
  }
}

const plural = (n: number, one: string): string => `${String(n)} ${n === 1 ? one : one + "s"}`;

/** The line under a passed check: "Folder Project · 12 files, 3 skipped.", with the cap when it was hit. */
export function boxCheckText(c: BoxCheck): string {
  const what = c.kind === "folder" ? "Folder" : "File";
  const count = plural(c.files, "file");
  const files = c.capped ? `${count} (the first ones; more were found)` : count;
  const skipped = c.skipped === 0 ? "" : `, ${String(c.skipped)} skipped (other types)`;
  return `${what} ${c.name} · ${files}${skipped}.`;
}

const TOAST: Record<string, string> = {
  box_consent_denied: "Box was not connected: access was not allowed.",
  box_state_invalid: "The Box sign-in expired. Try again.",
  box_code_invalid: "Box did not accept the sign-in. Try again.",
  box_unreachable: "Box did not answer. Try again in a minute.",
  box_auth_failed: "Box refused the request of this server. Ask the person who runs Graphico.",
  box_unavailable: "Box is not set up on this server.",
};

/** The toast of `?box=<code>` on the way back from Box. */
export function boxToast(code: string): { title: string; tone: "success" | "danger" } {
  if (code === "connected") return { title: "Box connected", tone: "success" };
  return { title: Object.hasOwn(TOAST, code) ? TOAST[code] : "Box could not be connected. Try again.", tone: "danger" };
}
