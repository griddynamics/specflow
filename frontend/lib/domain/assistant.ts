// FE-DATA-01, FE-BOT-03 (TECH-SPECS §2.11).
import type { DocRef } from "./common";

export type Citation = DocRef & { label: string };
export interface AssistantIntro {
  greeting: string;
  subline: string;
  placeholder: string;
  suggestions: string[];
} // MOCKS §7.1
export type AssistantEvent =
  | { type: "step"; stepId: string; label: string; status: "running" | "done" }
  | { type: "typing"; typing: boolean }
  | { type: "answer"; markdown: string }
  | { type: "citations"; items: Citation[] }
  | { type: "decision-link"; decisionId: string; label: string }
  | { type: "done" };

export interface AssistantPort {
  intro(projectId: string): Promise<AssistantIntro>;
  /** Events in order; ends with "done". Abort (leaving SpecBot) stops it and clears its timers. */
  ask(projectId: string, question: string, signal: AbortSignal): AsyncIterable<AssistantEvent>;
}
