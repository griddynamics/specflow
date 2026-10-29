// FE-BOT-01, FE-BOT-02, FE-BOT-03 (TECH-SPECS §2.11): pure state of one SpecBot exchange. Node-testable: no React, no fetch.
import type { AssistantEvent, Citation } from "@/lib/domain/assistant";

export interface BotStep {
  id: string;
  label: string;
  status: "running" | "done";
}

/** One exchange: the question and what SpecBot has shown so far. */
export interface Exchange {
  id: number;
  question: string;
  steps: BotStep[];
  typing: boolean;
  answer: string | null;
  citations: Citation[];
  decisionLink: { decisionId: string; label: string } | null;
  done: boolean;
  failed: boolean;
}

export function newExchange(id: number, question: string): Exchange {
  return { id, question, steps: [], typing: false, answer: null, citations: [], decisionLink: null, done: false, failed: false };
}

/** Applies one AssistantEvent. Returns a new object; unknown event types leave the exchange unchanged. */
export function applyEvent(x: Exchange, e: AssistantEvent): Exchange {
  switch (e.type) {
    case "step": {
      const known = x.steps.some((s) => s.id === e.stepId);
      const steps = known
        ? x.steps.map((s) => (s.id === e.stepId ? { ...s, label: e.label, status: e.status } : s))
        : [...x.steps, { id: e.stepId, label: e.label, status: e.status }];
      return { ...x, steps };
    }
    case "typing":
      return { ...x, typing: e.typing };
    case "answer":
      return { ...x, typing: false, answer: e.markdown };
    case "citations":
      return { ...x, citations: e.items };
    case "decision-link":
      return { ...x, decisionLink: { decisionId: e.decisionId, label: e.label } };
    case "done":
      return { ...x, typing: false, done: true };
  }
  return x; // an event type this client does not know leaves the exchange unchanged
}

/** The text a screen shows when the port fails (generic UI copy). */
export const BOT_ERROR_TEXT = "Something went wrong, so I could not answer. Please try again.";

/** The exchange a screen shows when `ask()` fails: the generic error text as the answer, no typing dots, finished. */
export function failExchange(x: Exchange): Exchange {
  return { ...x, typing: false, answer: BOT_ERROR_TEXT, failed: true, done: true };
}

/** True when the page is scrolled to within `slack` px of the bottom (auto-scroll only then). */
export function nearBottom(scrollHeight: number, scrollTop: number, viewport: number, slack = 160): boolean {
  return scrollHeight - scrollTop - viewport <= slack;
}

/** `![alt](url)` and `[text](url)` become their text (left to right, no overlap), like the pattern `!?\[([^\]]*)\]\([^)]*\)` -> `$1`. */
function linkTexts(markdown: string): string {
  let out = "";
  let from = 0; // the text before `from` is already in `out`
  let open = markdown.indexOf("[");
  while (open >= 0) {
    const close = markdown.indexOf("]", open + 1);
    if (close < 0) break; // no `]` after this `[`, nor after any later one
    if (markdown[close + 1] === "(") {
      const end = markdown.indexOf(")", close + 2);
      if (end >= 0) {
        const start = open > from && markdown[open - 1] === "!" ? open - 1 : open;
        out += markdown.slice(from, start) + markdown.slice(open + 1, close);
        from = end + 1;
        open = markdown.indexOf("[", from);
        continue;
      }
    }
    open = markdown.indexOf("[", open + 1);
  }
  return out + markdown.slice(from);
}

/** Plain text for the screen-reader announcement: no Markdown syntax (bold, italics, code, links, lists, headings). */
export function plainText(markdown: string): string {
  return linkTexts(markdown)
    .replace(/^\s{0,3}(?:#{1,6}|[*+>-]|\d+\.)\s+/gm, "")
    .replace(/\*\*|__|[*_`]|~~/g, "")
    .replace(/\s+/g, " ")
    .trim();
}
