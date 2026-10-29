// FE-DATA-01, FE-DEC-01 (TECH-SPECS §2.10).
import type { DocRef } from "./common";

export type Severity = "critical" | "high" | "medium" | "low";
export type QuestionType = "choice" | "yesno" | "upload";
export interface Question {
  id: string;
  number: string;
  text: string;
  help?: string;
  type: QuestionType;
  // label = option text WITHOUT the letter; the component adds A, B, C from the order. value = "A" | "B" | "C".
  options?: { value: string; label: string; description?: string }[];
  allowOther?: boolean; // adds "D: Other" with a text box (value "__other")
}
export type AnswerValue =
  { kind: "option"; value: string; otherText?: string } | { kind: "yesno"; value: "yes" | "no" } | { kind: "files"; fileNames: string[] };
export interface Decision {
  id: string;
  title: string;
  domain: string;
  severity: Severity;
  assigneeId: string;
  status: "open" | "answered";
  context: string; // Markdown
  docLinks: DocRef[]; // open in Files (§1.4)
  questions: Question[];
  // answered decisions (MOCKS §10.4): recorded answer and optional comment PER QUESTION id
  recorded?: Record<string, { answer: AnswerValue; comment?: string }>;
}
export interface DecisionFilters {
  openOnly: boolean;
  assignedToMe: boolean;
  domain: string; // "all" = every domain
  severity: Severity | "all";
}
export const DEFAULT_DECISION_FILTERS: DecisionFilters = {
  openOnly: true,
  assignedToMe: false,
  domain: "all",
  severity: "all",
}; // FE-DEC-01
/** One entry per answered question; the optional comment belongs to that question. */
export interface DecisionAnswer {
  decisionId: string;
  answers: Record<string, { value: AnswerValue; comment?: string }>;
}

export interface DecisionsPort {
  list(projectId: string): Promise<Decision[]>; // fixture order
  get(projectId: string, decisionId: string): Promise<Decision>;
  domains(projectId: string): Promise<string[]>; // Domain filter values, MOCKS §4 order
  submit(projectId: string, answer: DecisionAnswer): Promise<{ assigneeId: string }>; // never changes the list (FE-DEC-04)
}
