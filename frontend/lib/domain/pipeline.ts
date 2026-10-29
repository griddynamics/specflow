// FE-DATA-01, FE-MC-03 (TECH-SPECS §2.6). "Files selected" is screen state; the port starts at Processing.
import type { Unsubscribe } from "./common";
import type { DataError } from "./errors";

export type FileStage = "upload" | "read" | "extract" | "done"; // labels: MOCKS §10.3
export type GraphStage = "link" | "decisions" | "diagrams"; // sub-steps: MOCKS §10.3
export type RetryTarget = { kind: "file"; fileId: string } | { kind: "graph" };

export type PipelineEvent =
  | { type: "run.started"; runId: string; files: { fileId: string; name: string; sizeBytes: number }[] }
  | { type: "file.progress"; runId: string; fileId: string; stage: Exclude<FileStage, "done">; progress: number | null } // 0..1; null = indeterminate
  | { type: "file.done"; runId: string; fileId: string; documentId?: string; notice?: "duplicate" }
  | { type: "file.failed"; runId: string; fileId: string; error?: DataError; retryable: boolean } // no error = unfinished work, "Continue" (PA18; AUTO-13)
  | { type: "file.stuck"; runId: string; fileId: string; stage: Exclude<FileStage, "done"> }
  | { type: "file.dismissed"; runId: string; fileId: string }
  | { type: "graph.started"; runId: string; total: number }
  | { type: "graph.progress"; runId: string; stage: GraphStage; completed: number; total: number }
  | { type: "graph.failed"; runId: string; error?: DataError; retryable: boolean } // no error = "Continue" (PA18; AUTO-13)
  | { type: "graph.stuck"; runId: string }
  | { type: "run.ready"; runId: string }
  | { type: "run.reset" }; // flow reset (§5.2) -> Empty

export interface FileProgress {
  fileId: string;
  name: string;
  sizeBytes: number;
  stage: FileStage;
  progress: number | null;
  state: "active" | "done" | "failed" | "stuck";
  error?: DataError;
  retryable?: boolean;
  notice?: "duplicate";
  documentId?: string;
}
export interface GraphProgress {
  stage: GraphStage;
  completed: number;
  total: number;
  state: "active" | "failed" | "stuck";
  error?: DataError;
  retryable?: boolean;
}

export type MissionControlSnapshot =
  | { phase: "empty" }
  | { phase: "processing"; runId: string; files: FileProgress[]; graph: GraphProgress | null }
  | { phase: "attention"; runId: string; files: FileProgress[]; graph: GraphProgress | null } // a failure or stuck item, nothing running
  | { phase: "ready" };

export interface MissionControlPort {
  snapshot(projectId: string): Promise<MissionControlSnapshot>;
  /** Events for this project. Production polls only while a listener exists and work is running. */
  subscribe(projectId: string, listener: (e: PipelineEvent, next: MissionControlSnapshot) => void): Unsubscribe;
  start(projectId: string, files: File[]): Promise<{ runId: string }>; // "Create project" in Files selected
  retry(projectId: string, target: RetryTarget): Promise<void>; // failed or stuck item
  dismiss(projectId: string, fileId: string): Promise<void>; // "Remove" a failed file that cannot be retried (PLAN PA29)
  leave(projectId: string): void; // Mission Control unmounted (§5.2)
}
