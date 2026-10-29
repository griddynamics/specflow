// FE-PLAN-08 (DECISIONS P25-2): the fixed labels of the Develop tab. Not fixture text (the sample next task comes from `DevelopContent.nextTask`).
import type { AgentId } from "@/lib/domain/plan";

export const AGENT_OPTIONS: { value: AgentId; label: string }[] = [
  { value: "claude-code", label: "Claude Code" },
  { value: "cursor", label: "Cursor" },
  { value: "codex", label: "Codex" },
];

/** The "Connect" caption per agent: where the snippet goes (the teach file path comes from the content). */
export const CONNECT_CAPTION: Record<AgentId, string> = {
  "claude-code": "Save as .mcp.json",
  cursor: "Save as .cursor/mcp.json",
  codex: "Run this in your terminal",
};

export const COPY_LABEL = "Copy";
export const COPIED_LABEL = "Copied"; // MOCKS §15.8 copyDone
export const COPIED_TOAST = "Copied to clipboard"; // MOCKS §15.8 snippetCopied
export const COPY_FALLBACK = "Press Ctrl/Cmd+C"; // MOCKS §15.8 copyFallback
export const COPIED_MS = 2000;

/** The "Teach the agent" caption: Codex gets a section for an AGENTS.md that may already hold the user's rules (R25-08). */
export function teachCaption(agent: AgentId, path: string): string {
  return agent === "codex" ? `Add this section to ${path}` : `Save as ${path}`;
}
