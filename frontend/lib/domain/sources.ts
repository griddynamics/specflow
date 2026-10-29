// Mission Control sources (plans/mission-control-sources/DESIGN.md D1, D7-D11). The demo mock implements these ports in full; the
// production http adapter (lib/data/http/sources.ts, knowledge.ts) maps the lean sources API onto the same shapes (GDRIVE plan 07).
// The shapes follow the design's API resources (connections, project status, knowledge builds).
import type { Unsubscribe } from "./common";

/** What a pasted link can point to (parsed into ids on the server, never fetched). */
export type LinkKind = "github" | "gdrive";
/** Every kind of source: a link kind, or a Local files batch (DATA-SRC-0003). */
export type SourceKind = LinkKind | "upload";
/** A pasted link to a source. */
export interface SourceLink {
  kind: LinkKind;
  url: string;
}

/** DESIGN "States": needs access -> queued -> syncing -> synced | error | needs re-link. `paused` = rate limited (not failed). */
export type ConnectionState = "needs_access" | "queued" | "syncing" | "paused" | "synced" | "error" | "needs_relink";

/** DESIGN "States": fixed error vocabulary (never server text). */
export type SourceErrorCode = "access_denied" | "not_found" | "unsupported_url" | "rate_limited" | "too_large" | "export_failed";

/** What the card asks the user to do to give access (D3 pasted GitHub token). Drive needs no sign-in: the folder is shared with the Drive account. */
export type AccessNeed = "github_token";

// ---- Google Drive (GDUI units; plans/google-drive-integration/architecture-notes.md B5) ----
/** The closed reason set of a Drive test, save or import (INT-GDUI-0002). A reason is data, not an exception. */
export type DriveReason =
  "invalid_link" | "no_access" | "not_a_folder" | "already_connected" | "drive_unavailable" | "setup_error" | "too_slow" | "unknown_error";
export type DriveCategory =
  | "google_docs"
  | "google_slides"
  | "google_sheets"
  | "pdf"
  | "word"
  | "powerpoint"
  | "excel"
  | "images"
  | "videos"
  | "other_files"
  | "other_google"
  | "shortcuts";
export type ProcessingClass = "processed" | "stored_only" | "skipped";
export type DriveIdentity = { ok: true; email: string } | { ok: false; reason: DriveReason };
export interface DriveCategoryCount {
  category: DriveCategory;
  processingClass: ProcessingClass;
  count: number;
}
export type DriveTestResult =
  | { ok: true; folderId: string; folderName: string; folderCount: number; fileCount: number; categories: DriveCategoryCount[] }
  | { ok: false; reason: DriveReason };
export type DriveSaveResult = { ok: true; connection: Connection } | { ok: false; reason: DriveReason };
/** Import progress of one source or of the project (INT-GDUI-0004). Finished = processed, stored, skipped or error. */
export interface ImportProgress {
  listingFinished: boolean;
  total: number;
  finished: number;
}

// DATA-GDUI-0001: the order of the table; the summary orders bullets by class, then count, then label (FR-GDUI-0010).
export const CLASS_ORDER: ProcessingClass[] = ["processed", "stored_only", "skipped"];
export const CATEGORY_ORDER: DriveCategory[] = [
  "google_docs",
  "google_slides",
  "google_sheets",
  "pdf",
  "word",
  "powerpoint",
  "excel",
  "images",
  "videos",
  "other_files",
  "other_google",
  "shortcuts",
];
export const CATEGORY_LABEL: Record<DriveCategory, string> = {
  google_docs: "Google Docs",
  google_slides: "Google Slides",
  google_sheets: "Google Sheets",
  pdf: "PDF",
  word: "Word documents",
  powerpoint: "PowerPoint files",
  excel: "Excel files",
  images: "Images",
  videos: "Videos",
  other_files: "Other files",
  other_google: "Other Google files",
  shortcuts: "Shortcuts",
};

export interface Connection {
  id: string;
  kind: LinkKind;
  url: string; // as pasted; never fetched (DESIGN "Safety")
  label: string; // canonical target: "owner/repo" or the Drive folder/file name
  detail: string; // "main @ 3f2a1c9", "Folder", "File"
  ownerId: string; // the creator; sync always uses the creator's access (D4)
  state: ConnectionState;
  access: AccessNeed | null; // set while state is needs_access or needs_relink
  error: SourceErrorCode | null;
  /** While syncing or paused: files checked so far; `total` is null while the source lists its files. */
  progress: { done: number; total: number | null } | null;
  counts: { documents: number; skipped: number; failed: number };
  /** Drive import progress (FR-GDUI-0018); null = GitHub. Drive states: listing and importing = syncing, done = synced, failed = error. */
  importProgress: ImportProgress | null;
  /** Why a Drive import failed (state error); null otherwise. */
  failure: DriveReason | null;
  createdAt: string;
  lastSyncAt: string | null;
  nextSyncAt: string | null; // daily schedule (D6)
  resumeAt: string | null; // paused (rate limited) until
}

export type BuildMode = "first" | "update" | "rebuild";
export type BuildState = "queued" | "running" | "succeeded" | "partially_succeeded" | "failed" | "stopped";

export interface KnowledgeBuild {
  id: string;
  mode: BuildMode;
  state: BuildState;
  total: number; // documents (first, rebuild) or changes (update) in this build
  completed: number;
  current: string | null; // name of the document in work
  documentCount: number; // documents in the knowledge when this build is done
  startedAt: string;
  finishedAt: string | null;
}

/** The project's source and build state (DESIGN "States"); it picks the build button's label and the demo start points. */
export type KnowledgePhase = "no_sources" | "collecting" | "ready_to_build" | "building" | "up_to_date" | "sources_changed" | "attention";

/** D9: one server-side status read for the project. */
export interface ProjectSourcesStatus {
  phase: KnowledgePhase;
  ready: boolean; // D9: Ready = the first build succeeded; stays true when sources change
  /** Every source of the project in added order (FR-SRC-0006): Drive folders, GitHub repositories (demo) and Local files batches. */
  sources: Source[];
  /** All sources and uploads together (FR-GDUI-0021): listing done when every source listed; counts add up. */
  progress: ImportProgress;
  totals: { documents: number; skipped: number; failed: number; pending: number };
  changes: { new: number; changed: number; removed: number }; // since the last successful build
  attention: { failedItems: number; needsAccess: number; needsRelink: number; errors: number };
  /** What the build button does now; null = nothing to build (no documents, or up to date). */
  nextBuild: Exclude<BuildMode, "rebuild"> | null;
  build: KnowledgeBuild | null; // the running build
  lastBuild: KnowledgeBuild | null; // the last finished build
}

// ---- Sources, files and their states (DATA-SRC-0001/0002/0003; sources-ux SPECS §7.1) ----
/** DATA-SRC-0001 table 1: every file of every source is in exactly one of these. */
export type FileState = "waiting" | "processing" | "done" | "not_read" | "skipped" | "failed" | "already_in_project";
/** The closed reason set of a problem file; no server or Google text reaches the screen. */
export type FileReason =
  | "not_readable"
  | "not_imported"
  | "access_lost"
  | "drive_unavailable"
  | "read_failed"
  | "store_failed"
  | "source_failed"
  | "unknown"
  | "already_in_project";
/** A Drive category, or Markdown (GitHub and Local files). */
export type FileCategory = DriveCategory | "markdown";
/** What names a source on screen; also the holder of a file that is "already in" another source. */
export interface SourceRef {
  id: string;
  kind: SourceKind;
  name: string;
  nameSuffix: number;
  addedAt: string;
}
/** GitHub only (demo build): what the card and the details still need from the old connection. */
export type GithubDetail = Pick<Connection, "url" | "detail" | "state" | "access" | "error" | "resumeAt" | "lastSyncAt">;
export interface Source extends SourceRef {
  addedBy: { id: string; name: string };
  /** The source still lists its files: its progress is 0 (DATA-SRC-0002). */
  listing: boolean;
  /** A Local files batch that still takes files (its upload runs). Never true for a Drive source. */
  open: boolean;
  total: number;
  counts: Record<FileState, number>;
  /** Why the source failed as a whole (DATA-SRC-0001 table 2); null = it did not. */
  failure: DriveReason | null;
  github: GithubDetail | null;
}
export interface SourceFile {
  id: string;
  name: string;
  category: FileCategory;
  state: FileState;
  reason: FileReason | null;
  holder: SourceRef | null;
}
/** One page of a source's files (INT-SRC-0002), in group order: problems, Processing, Waiting, Done. */
export interface FilePage {
  source: Source;
  items: SourceFile[];
  nextCursor: string | null;
}
/** DATA-SRC-0002. */
export type SourceState = "processing" | "done" | "done_with_problems" | "failed";

/** What one "Add files" did (FR-SRC-0020..0022): the files picked, and what became of each. */
export interface UploadReport {
  sourceId: string | null; // null = no batch was made (the start failed)
  picked: number;
  added: number;
  alreadyInProject: number;
  readAgain: number;
  duplicates: number; // the same content twice in the batch: nothing new
  notAdded: number; // refused, or the batch ended before the file got in
}

/** Collect side (D1, D6, D7): sources, sync, access, uploads and removal. The status read carries the counts per source; `files` the
 * file list of one source (sources-ux). */
export interface SourcesPort {
  /** Whether "GitHub repository" is offered in Add source (owner K3: hidden in production, where no GitHub backend exists). */
  readonly githubEnabled: boolean;
  /** Local files accept PDF only (production: the upload API reads PDF). The demo mock also lists Markdown and other files. */
  readonly pdfOnly: boolean;
  /** GitHub only (Drive goes through `testDrive` and `saveDrive`). Idempotent per canonical target: an existing connection comes back with `created: false`. A bad link is a connection in error. */
  add(projectId: string, link: SourceLink): Promise<{ connection: Connection; created: boolean }>;
  /** Removes the source and its files (FR-SRC-0016). Already gone ⇔ rejects `DataError("not_found", false, { apiCode: "source_not_found" })`. */
  remove(projectId: string, sourceId: string): Promise<void>;
  /** One page of a source's files (INT-SRC-0002); `cursor` comes from the page before. Unknown source: the same rejection as `remove`. */
  files(projectId: string, sourceId: string, cursor?: string | null): Promise<FilePage>;
  /** 202-like: a click during a run is coalesced. */
  syncNow(projectId: string, connectionId: string): Promise<void>;
  /** Phase 1: a simulated token check; the token is never stored. */
  addToken(projectId: string, connectionId: string, token: string): Promise<void>;
  /** One Local files batch: start, add the files one by one, end (INT-SRC-0004). The build button processes them (D7). */
  upload(projectId: string, files: File[]): Promise<UploadReport>;
  /** The Drive account email to share folders with (FR-GDUI-0002). A failure is a reason, not an exception. */
  driveIdentity(): Promise<DriveIdentity>;
  /** Lists the folder, creates nothing (FR-GDUI-0009). Transport failures reject; the modal shows them as `unknown_error`. */
  testDrive(projectId: string, link: string): Promise<DriveTestResult>;
  /** Adds the folder as a source and starts its import (FR-GDUI-0017). The UI owns the 60 s and 30 s cutoffs. */
  saveDrive(projectId: string, link: string): Promise<DriveSaveResult>;
}

/** The source-aware Mission Control port (D9, D11): project status and knowledge builds. */
export interface KnowledgePort {
  /** Whether `build` does anything. Production: false (Q12), a click only says "not available yet". The demo: true. */
  readonly buildAvailable: boolean;
  status(projectId: string): Promise<ProjectSourcesStatus>;
  /** Called after any change of the project's sources, files or builds; read `status` again. */
  subscribe(projectId: string, listener: () => void): Unsubscribe;
  /** One build at a time per project; `first` before Ready, `update` / `rebuild` after. */
  build(projectId: string, mode: BuildMode): Promise<KnowledgeBuild>;
  /** Mission Control unmounted. Work keeps running at the source side; only screen listeners stop. */
  leave(projectId: string): void;
}
