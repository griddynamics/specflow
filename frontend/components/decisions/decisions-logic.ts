// FE-DEC-01, FE-DEC-03, FE-DEC-04 (PLAN a.8, MOCKS §10): pure helpers of the Decisions screen. No React, no fetch.
import type { DocRef } from "@/lib/domain/common";
import type { AnswerValue, Decision, DecisionAnswer, DecisionFilters, Severity } from "@/lib/domain/decisions";

/** The value of the "Other" card in OptionCards (design-system `OTHER_VALUE`); repeated here so this file stays node-testable. */
export const OTHER = "__other";

export const SEVERITY_OPTIONS: { value: Severity | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "critical", label: "Critical" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

/** "All" first, then the port's domain values in their order. */
export function domainOptions(domains: string[]): { value: string; label: string }[] {
  return [{ value: "all", label: "All" }, ...domains.map((d) => ({ value: d, label: d }))];
}

/** A severity value coming from the segmented control; anything unknown means "all". */
export function toSeverityFilter(v: string): DecisionFilters["severity"] {
  return v === "critical" || v === "high" || v === "medium" || v === "low" ? v : "all";
}

/** The count line announced to assistive technology (aria-live). */
export function countText(shown: number, total: number): string {
  return `${String(shown)} of ${String(total)} ${total === 1 ? "decision" : "decisions"}`;
}

/** A doc-link `label` is "Document title › Section heading" (MOCKS §14). Falls back to the path / anchor. */
export function docLinkParts(ref: DocRef): { title: string; path: string; section?: string } {
  const label = ref.label ?? "";
  const i = label.indexOf(" › ");
  const title = i < 0 ? label : label.slice(0, i);
  const section = i < 0 ? undefined : label.slice(i + 3);
  return { title: title || ref.path, path: ref.path, section: section ?? ref.anchor };
}

/** What the user has entered for one question (UI state). `other` is kept when the user moves to another option. */
export interface QuestionDraft {
  choice?: string;
  other: string;
  yesno?: "yes" | "no";
  files: string[];
  comment: string;
}
export const emptyDraft = (): QuestionDraft => ({ other: "", files: [], comment: "" });

/**
 * The DecisionAnswer for the submit call: one entry per ANSWERED question (PA15: unanswered questions are allowed). The "Other" text
 * counts only while "Other" is the chosen option. A comment belongs to its question (PA26); a comment on an unanswered question has
 * no answer to attach to and is not sent.
 */
function isFilled(text: string | undefined): text is string {
  return text !== undefined && text !== "";
}

/** True when the draft holds an answer for the question (a comment alone is not an answer). */
export function hasAnswer(q: Decision["questions"][number], d: QuestionDraft | undefined): boolean {
  if (!d) return false;
  if (q.type === "choice") return isFilled(d.choice);
  if (q.type === "yesno") return d.yesno !== undefined;
  return d.files.length > 0;
}

/** The value one draft contributes for its question, or null when the question is unanswered. */
function draftValue(q: Decision["questions"][number], d: QuestionDraft): AnswerValue | null {
  if (q.type === "choice" && isFilled(d.choice)) {
    return d.choice === OTHER ? { kind: "option", value: OTHER, otherText: d.other.trim() } : { kind: "option", value: d.choice };
  }
  if (q.type === "yesno" && d.yesno !== undefined) return { kind: "yesno", value: d.yesno };
  if (q.type === "upload" && d.files.length > 0) return { kind: "files", fileNames: [...d.files] };
  return null;
}

export function buildAnswer(decision: Decision, drafts: Record<string, QuestionDraft | undefined>): DecisionAnswer {
  const answers: DecisionAnswer["answers"] = {};
  for (const q of decision.questions) {
    const d = drafts[q.id];
    if (!d) continue;
    const value = draftValue(q, d);
    if (value === null) continue;
    const comment = d.comment.trim();
    answers[q.id] = comment === "" ? { value } : { value, comment };
  }
  return { decisionId: decision.id, answers };
}

/** MOCKS §10.1 `decisionSubmitted`: "Answer submitted. {first name} will be notified."; the active user as assignee: "Answer submitted." */
export function submitToastTitle(assigneeId: string, meId: string | undefined, nameOf: (id: string) => string | undefined): string {
  if (assigneeId === meId) return "Answer submitted.";
  const first = nameOf(assigneeId)?.trim().split(/\s+/)[0];
  return isFilled(first) ? `Answer submitted. ${first} will be notified.` : "Answer submitted.";
}

/** The text a recorded answer shows (option letter, Yes/No, file names): for tests and accessible summaries. */
export function recordedSummary(a: AnswerValue): string {
  if (a.kind === "option") return a.value === OTHER ? `Other: ${a.otherText ?? ""}` : a.value;
  if (a.kind === "yesno") return a.value === "yes" ? "Yes" : "No";
  return a.fileNames.join(", ");
}
