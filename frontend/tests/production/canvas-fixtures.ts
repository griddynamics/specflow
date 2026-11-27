// The inputs of the canvas tests: a sources status as the http adapter derives it, the gap summary, the plan state, one `CanvasInput`.
// Shared by the model and the render tests so a scene is described once.
import { deriveStatus, NO_GRAPH, toStatus, type GraphBuildWire } from "@/lib/data/http/knowledge";
import type { CanvasInput, GapsSummary } from "@/components/v2/canvas/canvas-types";
import type { Gap } from "@/lib/domain/gaps";
import type { PlanState, PlanStory, PlanTask } from "@/lib/domain/plan";
import type { ProjectRepositories, ProjectRepository } from "@/lib/domain/github";
import type { Integration } from "@/lib/domain/integrations";
import type { JiraSettings } from "@/lib/domain/jira";
import type { ProjectSourcesStatus } from "@/lib/domain/sources";

const COUNTS = { waiting: 0, processing: 0, done: 0, not_read: 0, skipped: 0, failed: 0, already_in_project: 0 };

export const NOW = new Date("2026-10-26T09:05:00Z");
export const STARTED = "2026-10-26T09:00:00Z";
export const FINISHED = "2026-10-26T09:12:00Z";

const NAMES = {
  gdrive: "Specs folder",
  upload: "Local files",
  confluence: "Ordering space",
  jira_epic: "NWO-12: Checkout redesign",
  box: "Project files",
  dropbox: "Project files",
} as const;

/** One source of the sources API (`GET /sources?batches=1`): `kind` upload or gdrive, files by state. */
export const wireSource = (
  id: string,
  kind: "upload" | "gdrive" | "confluence" | "jira_epic" | "box" | "dropbox",
  counts: Partial<typeof COUNTS>,
  over: Record<string, unknown> = {},
) => {
  const c = { ...COUNTS, ...counts };
  return {
    id,
    kind,
    name: NAMES[kind],
    name_suffix: 1,
    added_by: { id: "u1", display_name: "dev" },
    created_at: `2026-10-26T08:0${id.slice(-1)}:00Z`,
    listing_finished: true,
    open: false,
    total: Object.values(c).reduce((n, x) => n + x, 0),
    counts: c,
    failure_reason: null,
    folder_id: kind === "gdrive" ? "f1" : null,
    link: null,
    ...over,
  };
};

export const graphWire = (over: Partial<GraphBuildWire> = {}): GraphBuildWire => ({ ...NO_GRAPH.graph, ...over });
export const RUNNING = graphWire({ status: "running", buildId: "b1", mode: "first", startedAt: STARTED });
export const SUCCEEDED = graphWire({ status: "succeeded", buildId: "b1", mode: "first", startedAt: STARTED, finishedAt: FINISHED });

export function statusOf(
  sources: ReturnType<typeof wireSource>[],
  graph: GraphBuildWire = NO_GRAPH.graph,
  processed = 4,
): ProjectSourcesStatus {
  const total = sources.reduce((n, s) => n + s.total, 0);
  const open = sources.reduce((n, s) => n + s.counts.waiting + s.counts.processing, 0);
  const body = {
    progress: { listing_finished: sources.every((s) => s.listing_finished), total, finished: total - open },
    processed_documents: processed,
    removed_files: 0,
    sources,
  };
  return deriveStatus(toStatus(body), { ...NO_GRAPH, graph });
}

export const GAPS: GapsSummary = { phase: "done", stepsDone: 4, stepsTotal: 4, total: 14, answered: 2, critical: 3, more: false };
const story = (storyId: string): PlanStory => ({
  storyId,
  areaId: "a1",
  kind: "feature",
  title: storyId,
  summary: null,
  progress: "pending",
});
const task = (taskId: string): PlanTask => ({
  taskId,
  areaId: "a1",
  storyId: "s1",
  title: taskId,
  description: "",
  phase: "logic",
  complexity: "low",
  status: "pending",
  doneWhen: [],
  waitsFor: [],
  implements: [],
  commit: null,
  blockReason: null,
  ready: true,
  plannedBy: "model",
  body: "",
  version: 1,
});
/** A plan with two stories and one task. */
export const READY_PLAN: PlanState = {
  phase: "ready",
  plan: { planId: "p1", areas: [], stories: [story("s1"), story("s2")], tasks: [task("t1")], quality: null, review: [] },
};

export const input = (over: Partial<CanvasInput> = {}): CanvasInput => ({
  now: NOW,
  pending: { build: false, plan: false },
  status: null,
  gaps: null,
  gapRows: [],
  plan: null,
  planStale: false,
  integrations: null,
  github: null,
  jira: null,
  githubProjects: null,
  notionPlan: null,
  jiraGaps: null,
  slack: null,
  linearPlan: null,
  linearGaps: null,
  ...over,
});

/** Scene 1 to 3 of docs/mission-control/README.md "The flow". */
export const SCENE_1 = input({ status: statusOf([], NO_GRAPH.graph, 0) });
export const SCENE_2 = input({
  status: statusOf([wireSource("s1", "gdrive", { done: 6, processing: 18 }), wireSource("s2", "upload", { done: 4, waiting: 2 })]),
});
export const SCENE_3 = input({
  status: statusOf([wireSource("s1", "gdrive", { done: 24 }), wireSource("s2", "upload", { done: 6 })], NO_GRAPH.graph, 30),
});
/** The graph is built (first build succeeded). */
export const BUILT = (over: Partial<CanvasInput> = {}) =>
  input({ status: statusOf([wireSource("s1", "gdrive", { done: 24 }), wireSource("s2", "upload", { done: 6 })], SUCCEEDED, 30), ...over });

/** A repository of the project, cloned unless said otherwise. */
export const repo = (over: Partial<ProjectRepository> = {}): ProjectRepository => ({
  id: "r1",
  githubRepoId: 101,
  fullName: "northwind/ordering",
  htmlUrl: "https://github.com/northwind/ordering",
  private: false,
  addedByName: "Alex Kim",
  addedByMe: false,
  cloneState: "cloned",
  failureCode: null,
  defaultBranch: "main",
  headSha: "3f2a1c9aaaa",
  fileCount: 18,
  sizeBytes: null,
  snapshotBytes: null,
  checkedAt: null,
  clonedAt: null,
  createdAt: "2026-10-26T08:10:00Z",
  ...over,
});
export const repos = (list: ProjectRepository[], canManage = true): ProjectRepositories => ({
  enabled: true,
  canManage,
  repositories: list,
});

/** An integration of the project: active, connected by Alex Kim, unless said otherwise. */
export const integration = (app: Integration["app"], over: Partial<Integration> = {}): Integration => ({
  app,
  available: true,
  connectedBy: { userId: "u9", name: "Alex Kim" },
  workspace: { id: "w1", name: "Northwind" },
  connectedAt: "2026-10-25T10:00:00Z",
  health: "active",
  ...over,
});

/** The Jira target of the project: NWO, epic NWO-1, on, never synced, acting as Alex Kim with a working connection. */
export const jiraTarget = (over: Partial<JiraSettings> = {}): JiraSettings => ({
  available: true,
  editable: true,
  siteUrl: "https://northwind.atlassian.test",
  jiraProject: "NWO",
  epicKey: "NWO-1",
  createdEpicKey: null,
  enabled: true,
  sync: null,
  connection: { connectedByName: "Alex Kim", connectedByMe: false, status: "active" },
  ...over,
});

/** A gap of the Gaps read: a missing-information question with the given status and severity. */
export const gapOf = (status: Gap["status"], severity: Gap["severity"], over: Partial<Gap> = {}): Gap => ({
  id: "g",
  kind: "missing",
  severity,
  status,
  origin: "analysis",
  route: "ask",
  title: "t",
  question: "q",
  why: "w",
  blocking: false,
  createdAt: "",
  updatedAt: "",
  about: [],
  impacts: [],
  witness: [],
  options: [],
  clarification: null,
  reach: null,
  assignedTo: null,
  ...over,
});
