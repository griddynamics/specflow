// MCP-M10: the agents of the MCP guide and the `?agent=` deep link. Pure.
export const AGENTS = [
  { value: "claude-code", label: "Claude Code", tested: true },
  { value: "codex", label: "Codex", tested: false },
  { value: "cursor", label: "Cursor", tested: false },
] as const;

export type AgentId = (typeof AGENTS)[number]["value"];
export const DEFAULT_AGENT: AgentId = "claude-code";

/** `?agent=` value to an agent; missing, repeated or unknown falls back to Claude Code. */
export function parseAgent(value: string | string[] | undefined): AgentId {
  const v = Array.isArray(value) ? value[0] : value;
  return AGENTS.find((a) => a.value === v)?.value ?? DEFAULT_AGENT;
}

export const agentLabel = (id: AgentId): string => AGENTS.find((a) => a.value === id)?.label ?? "Claude Code";
/** Only tested agents show setup steps. */
export const isTested = (id: AgentId): boolean => AGENTS.find((a) => a.value === id)?.tested === true;
