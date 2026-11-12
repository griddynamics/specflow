// MCP-TOOLING session 10 (docs/graph/gap.md "Web app", docs/mcp-server/access-control.md AC12): the findings list and the claims board of
// the Plan page. Findings are what agents report about the spec (a gap, a conflict, a question, a note); people resolve or dismiss them.
// Claims are who works on which story or task; a forgotten story claim is freed by a forced release. Types and the port only.

export type FindingKind = "gap" | "conflict" | "question" | "note";
export type FindingStatus = "open" | "resolved" | "dismissed";
export type FindingFilter = FindingStatus | "all";
export type RefType = "pr" | "commit" | "file" | "url";

/** A person behind a finding or a claim: a display name (null: not known), never an id or an email. `you` marks the signed-in person. */
export interface Person {
  name: string | null;
  you: boolean;
  /** The label an agent gave its claim ("claude-code"); claims only. */
  agentLabel: string | null;
}

export interface FindingRef {
  type: RefType;
  value: string;
}

export interface Finding {
  findingId: string;
  kind: FindingKind;
  text: string;
  status: FindingStatus;
  /** A blocking finding stops its story until a person answers it. */
  blocking: boolean;
  /** The spec nodes the finding is about (ids). */
  about: string[];
  refersTo: FindingRef[];
  storyId: string | null;
  /** The title of that story (open blocking findings only); null when not known. */
  storyTitle: string | null;
  author: Person;
  createdAt: string;
  resolvedBy: Person | null;
  resolvedAt: string | null;
  resolutionNote: string | null;
}

/** One page of findings, blocking first, then newest first; `more` says the project has more than the page holds. */
export interface FindingList {
  findings: Finding[];
  more: boolean;
}

export type ClaimKind = "story" | "task";

export interface Claim {
  kind: ClaimKind;
  storyId: string;
  /** The story title for a story claim, the task title for a task claim. */
  title: string;
  taskId: string | null;
  holder: Person;
  claimedAt: string;
  branch: string | null;
  /** Tasks held under the claim (a story claim holds its whole unit). */
  tasks: number;
}

export type CloseAction = "resolve" | "dismiss";

export interface WorkPort {
  /** The findings of the project (status `open` unless asked), optionally about one spec node. */
  findings(projectId: string, opts?: { status?: FindingFilter; node?: string }): Promise<FindingList>;
  /** Resolves or dismisses an open finding with an optional note; `unblocked` counts the tasks that went back to pending. */
  close(projectId: string, findingId: string, action: CloseAction, note?: string): Promise<{ finding: Finding; unblocked: number }>;
  /** The claims, oldest first; a project without a plan has none. */
  claims(projectId: string): Promise<Claim[]>;
  /** Frees a story claim by force; `released` counts the tasks that went back to pending (0: the claim was already gone). */
  release(projectId: string, storyId: string): Promise<{ released: number }>;
}
