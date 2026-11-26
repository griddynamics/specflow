// The words and rules of the diagrams.net link source (docs/sources/importers.md "diagrams.net link"), free of React: what a failed check or
// add says (the closed codes of the API), what a passed check says, and the form's own labels.
import { isDataError } from "@/lib/domain/errors";
import type { DrawioCheck } from "@/lib/domain/sources";
import { V2_COPY } from "../v2-copy";

export const DRAWIO_COPY = {
  link: "Diagram link",
  /** "Show me how": what a person does in diagrams.net to get the link (the viewer link holds the whole diagram). */
  how: [
    "In diagrams.net, open the diagram and choose File → Publish → Link.",
    "Copy the link it shows.",
    "Paste it here and press Check link.",
  ],
} as const;

/** Where a diagram that is not in the link is kept, by the letter after the `#` of its address. */
const STORAGE: Record<string, string> = {
  G: "Google Drive",
  H: "GitHub",
  A: "GitLab",
  W: "OneDrive",
  T: "Trello",
  D: "Dropbox",
  L: "your browser",
};

/** The storage a link points to, or "a storage that cannot be read" when the letter is not one of draw.io's. */
export function drawioStorage(link: string): string {
  let letter = "";
  try {
    letter = new URL(link.trim()).hash.charAt(1);
  } catch {
    // not a link: no storage
  }
  return Object.hasOwn(STORAGE, letter) ? STORAGE[letter] : "a storage that cannot be read";
}

const FAILURE: Record<string, string> = {
  drawio_link_invalid: "Not a diagrams.net link",
  drawio_fetch_failed: "The diagram could not be fetched. Check that the file is public and try again.",
  drawio_not_a_diagram: "The link does not hold a diagram.",
  drawio_too_large: "The diagram is too large to read.",
};

/** What a failed check or add says, from the API's closed code and the link it was made with; anything else is the general failure. */
export function drawioFailureText(e: unknown, link: string): string {
  const code = isDataError(e) ? (e.detail?.apiCode ?? "") : "";
  if (code === "drawio_link_unsupported") {
    return `This diagram is stored in ${drawioStorage(link)}. Download it as .drawio (File → Export as → XML / .drawio) and add it with Upload.`;
  }
  return Object.hasOwn(FAILURE, code) ? FAILURE[code] : V2_COPY.actionFailed;
}

const plural = (n: number, one: string): string => `${String(n)} ${n === 1 ? one : one + "s"}`;

/** The line under a passed check: "Release flow · 2 pages · 53 shapes", and a note when a cap (pages or shapes) cut the diagram. */
export function drawioCheckText(c: DrawioCheck): string {
  const shapes = c.pages.reduce((n, p) => n + p.shapes, 0);
  const line = `${c.name} · ${plural(c.pages.length, "page")} · ${plural(shapes, "shape")}`;
  return c.capped ? `${line}. Only part of this diagram is read.` : line;
}
