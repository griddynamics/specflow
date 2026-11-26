// Stub of the SPECS §9 HTTP API, close enough to drive the pages and the BFF.
// It is a test double only: the real API is built in S7. In-memory, one process.
import http from "node:http";
import crypto from "node:crypto";
import { createLocalJWKSet, jwtVerify } from "jose";
import { listen, close } from "./servers.mjs";
import { createSourcesModel, StubError } from "./stub-sources.mjs";
import { checkConfluence, SITE_HOST } from "./stub-confluence.mjs";
import { checkLinearSource } from "./stub-linear-source.mjs";
import { checkJiraEpic } from "./stub-jira-epic.mjs";
import { driveFilesCapped, driveFilesOf } from "./stub-drive-files.mjs";
import { createJiraModel } from "./stub-jira.mjs";
import { createLinearModel } from "./stub-linear.mjs";
import { createGitHubProjectsModel } from "./stub-github-projects.mjs";
import { createLinearPlanModel } from "./stub-linear-plan.mjs";
import { createJiraGapsModel } from "./stub-jira-gaps.mjs";
import { createLinearGapsModel } from "./stub-linear-gaps.mjs";
import { createGitHubModel } from "./stub-github.mjs";
import { bindingRefusal, checkNotion, createNotionModel, createNotionPlanModel } from "./stub-notion.mjs";
import { createIntegrationsModel } from "./stub-integrations.mjs";
import { createMembersModel, ME, StubError as MembersError } from "./stub-members.mjs";

const LEEWAY_SECONDS = 60; // as backend/src/auth/claims.py
const DEFAULT_CLIENT_ID = "specflow-web";

const json = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};
const err = (res, status, code, details = {}) => json(res, status, { error: { code, message: code, details } });

// The upload table of the API (backend/src/file_parsers/formats.py, src/api/uploads.py MAX_BYTES). The extension picks the type; the
// stub is STRICTER than the API on the header: it wants the canonical type exactly, so a frontend that sends `file.type` (empty for
// .md, application/vnd.ms-excel for .csv on Windows) fails here. tests/production/upload-formats.test.ts checks this table against
// the backend's.
const MB = 1024 * 1024;
const OOXML = "application/vnd.openxmlformats-officedocument";
export const UPLOAD_TABLE = {
  ".pdf": { type: "application/pdf", cap: 100 * MB, magic: "%PDF-" },
  ".docx": { type: `${OOXML}.wordprocessingml.document`, cap: 50 * MB, magic: "PK\x03\x04" },
  ".pptx": { type: `${OOXML}.presentationml.presentation`, cap: 50 * MB, magic: "PK\x03\x04" },
  ".xlsx": { type: `${OOXML}.spreadsheetml.sheet`, cap: 50 * MB, magic: "PK\x03\x04" },
  ".html": { type: "text/html", cap: 10 * MB, magic: null },
  ".md": { type: "text/markdown", cap: 10 * MB, magic: null },
  ".csv": { type: "text/csv", cap: 10 * MB, magic: null },
};
const extensionOf = (name) => {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot).toLowerCase() : "";
};

// ---- Drive sources (GDRIVE plan 04, architecture-notes B4): a close-enough double of the sources endpoints ----
const REASON_HTTP = {
  invalid_link: 422,
  not_a_folder: 422,
  no_access: 409,
  already_connected: 409,
  drive_unavailable: 503,
  setup_error: 503,
  too_slow: 504,
  unknown_error: 502,
};
const FOLDER_LINK = /^https:\/\/drive\.google\.com\/drive\/folders\/([A-Za-z0-9_-]{10,})(?:\?.*)?$/;
const SAMPLE = {
  default: {
    folder_count: 3,
    categories: [
      ["google_docs", "processed", 5],
      ["pdf", "processed", 3],
      ["word", "stored_only", 2],
      ["shortcuts", "skipped", 1],
    ],
  },
  "fake-empty-folder": { folder_count: 1, categories: [] },
  "fake-forms-only": { folder_count: 1, categories: [["other_google", "skipped", 2]] },
  "fake-capped-folder": { folder_count: 2, categories: [["pdf", "processed", 1000]] },
};
const PARSED = new Set(["parsed", "ingested", "partially_ingested"]);

/**
 * @param {{port:number, verify?:{issuer:string, audience:string, jwks:{keys:object[]}, clientIds?:string[]}, parseMs?:number, summarizeMs?:number}} opts
 * `verify` makes the API check every bearer against the issuer's JWKS, as backend/src/auth/claims.py `validate` does: RS256 signature,
 * `iss` equal to `issuer`, `aud` equal to `audience`, `exp` not past, `iat` not in the future (both with 60 s leeway), `azp` one of
 * `clientIds` (default: the web client "specflow-web"), and `email_verified` true. Anything else is a 401. `typ` is not checked.
 * Without `verify` any bearer is accepted (the open mode of the contract tests).
 * parseMs/summarizeMs = Infinity keeps the work "in progress" forever (stuck tests).
 * Knowledge build knobs on `state` (see `graphRoute`): graphMs (a build resolves when it has run that long; Infinity = never),
 * graphOutcome ("succeeded" | "partially_succeeded" | "failed") and graphStartFail (null | an API code: the POST answers 503 with it).
 * `sourcesFail` (an API code) makes the sources status read answer 503 with it.
 * Gap knob: gapMs (the gap run a finished build queues completes after that long; Infinity = never).
 * Plan knob: planMs (a plan run takes that long, its ten steps evenly spread; see `planRoute`).
 */
export async function startStubApi({ port, verify, parseMs = 1500, summarizeMs = 1500, parseOutcome = "parsed" }) {
  const keys = verify ? createLocalJWKSet(verify.jwks) : undefined;
  const projects = new Map();
  const docs = new Map(); // id -> doc
  const log = []; // {method, path, headers, authOk, contentType, bytes}
  const state = {
    parseMs,
    summarizeMs,
    parseOutcome,
    graphMs: 1500,
    gapMs: 1000,
    graphOutcome: "succeeded",
    graphStartFail: null,
    planMs: 2000,
    workFail: null,
    workReadFail: null,
    gapFail: null,
    sourcesFail: null,
  };
  /** Drive: sources per project (SourceOut rows, mutable by tests), and the identity answer (a Reason string = 503 with that code). */
  const sources = new Map(); // pid -> SourceOut[]
  const drive = { identityReason: null, email: "specflow-sa@example.test" };
  const sourcesOf = (pid) => sources.get(pid) ?? sources.set(pid, []).get(pid);
  /** sources-ux (SPECS §4): the v2 status, file list, removal and Local files batches. Documents of an upload carry `batchId`. */
  const model = createSourcesModel({ docs, sourcesOf });
  /** Project collaborators (docs/projects/api.md): members, invites, leave, rename, suggestions, create with collaborators. */
  const people = createMembersModel();
  /** Jira settings (docs/jira-sync/api.md#web-routes): the target of a project, the owner rule, scripted access checks. */
  const jira = createJiraModel((pid) => people.list(pid).role);
  /** GitHub (docs/github/api.md): the caller's connection, their repositories, a project's repositories. */
  const github = createGitHubModel((pid) => people.list(pid).role, ME.name);
  /** Notion (docs/notion/oauth.md): the caller's connection. */
  const notion = createNotionModel();
  /** Linear (plans/LINEAR): the caller's connection. */
  const linear = createLinearModel(ME.name);
  /** Project integrations (docs/mission-control/integrations-api.md): who each app is connected by, and the Slack channel of a project. */
  const integrations = createIntegrationsModel({
    roleOf: (pid) => people.list(pid).role,
    jira,
    github,
    linear,
    notion,
    displayName: ME.name,
  });
  /** Notion plan destination: any member; acts as the project's Notion binding. */
  const notionPlan = createNotionPlanModel((pid) => integrations.notionHealth(pid));
  /** The Linear gap channel: any member; acts as the project's Linear binding (the integrations stub, once it knows Linear, says who). */
  const linearGaps = createLinearGapsModel((pid) => integrations.linearActive?.(pid) ?? true);
  /** The Jira gap channel: any member; acts as the project's Atlassian binding. */
  const jiraGaps = createJiraGapsModel((pid) => integrations.atlassianActive(pid));
  /** GitHub Projects plan destination: any member; acts as the project's GitHub binding; the repository is one of the project's. */
  const githubProjects = createGitHubProjectsModel(
    (pid) => integrations.githubActive(pid),
    (pid, id) => github.list(pid).repositories.find((r) => r.id === id),
  );
  /** Linear plan destination: any member; acts as the project's Linear binding (bound while the integrations stub has no Linear model). */
  const linearPlan = createLinearPlanModel(
    (pid) => (typeof integrations.linearActive === "function" ? integrations.linearActive(pid) : true),
    (pid) => projects.get(pid)?.name ?? "Project",
  );
  const readJson = async (req) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const text = Buffer.concat(chunks).toString();
    try {
      return text === "" ? {} : JSON.parse(text);
    } catch {
      throw new MembersError(422, "validation_failed", { errors: [{ loc: ["body"], type: "json_invalid" }] });
    }
  };
  const drainBody = async (req) => {
    for await (const c of req) void c;
  };
  const progressOf = (pid) => {
    const parts = sourcesOf(pid).map((c) => c.progress);
    const mine = [...docs.values()].filter((d) => d.pid === pid);
    const open = (d) => d.row.status === "processing" || d.row.status === "uploaded";
    const uploads = mine.length
      ? {
          progress: { listing_finished: true, total: mine.length, finished: mine.filter((d) => !open(d)).length },
          documents: mine.length,
          failed: mine.filter((d) => d.row.status === "failed").length,
        }
      : null;
    if (uploads) parts.push(uploads.progress);
    return {
      progress: {
        listing_finished: parts.every((p) => p.listing_finished),
        total: parts.reduce((n, p) => n + p.total, 0),
        finished: parts.reduce((n, p) => n + p.finished, 0),
      },
      sources: sourcesOf(pid),
      uploads,
      processed_documents: mine.filter((d) => PARSED.has(d.row.status)).length,
    };
  };
  // Knowledge build (`/graph_build`): one state per project, idle at first. No timer: a running build is resolved when it is read.
  const graphs = new Map(); // pid -> {status, build_id, mode, started_at, finished_at, error_code, startedMs, succeededBefore}
  let graphSeq = 0;
  const graphOf = (pid) => {
    const g = graphs.get(pid) ?? { status: "idle", build_id: null, mode: null, started_at: null, finished_at: null, error_code: null };
    if (g.status === "running" && Date.now() - g.startedMs >= state.graphMs) {
      g.status = state.graphOutcome;
      g.finished_at = new Date().toISOString();
      if (g.status === "failed") g.error_code = "stub_failure";
      else {
        g.succeededBefore = true;
        queueGapRun(pid);
      }
    }
    graphs.set(pid, g);
    return g;
  };
  const graphWire = ({ status, build_id, mode, started_at, finished_at, error_code }) => ({
    status,
    build_id,
    mode,
    started_at,
    finished_at,
    error_code,
  });
  /** The POST body is empty or JSON whose only key is `reasoning_level`: null or an integer 1..5 (the backend's GraphBuildRequest). */
  const validGraphBody = (text) => {
    if (text.trim() === "") return true;
    try {
      const b = JSON.parse(text);
      if (typeof b !== "object" || b === null || Array.isArray(b)) return false;
      const keys = Object.keys(b);
      if (keys.some((k) => k !== "reasoning_level")) return false;
      const n = b.reasoning_level;
      return keys.length === 0 || n === null || (Number.isInteger(n) && n >= 1 && n <= 5);
    } catch {
      return false;
    }
  };
  async function graphRoute({ req, res, pid, entry }) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    if (req.method === "GET") return json(res, 200, graphWire(graphOf(pid)));
    if (req.method !== "POST") return err(res, 405, "method_not_allowed");
    const text = Buffer.concat(chunks).toString();
    if (!validGraphBody(text)) return err(res, 422, "validation_failed");
    entry.bodyText = text;
    if (state.graphStartFail) return err(res, 503, state.graphStartFail);
    if (model.status(pid).processed_documents === 0) return err(res, 409, "no_processed_documents");
    const g = graphOf(pid);
    if (g.status === "running") return json(res, 202, graphWire(g));
    const now = Date.now();
    graphs.set(pid, {
      status: "running",
      build_id: `gb-${String(++graphSeq)}`,
      mode: g.succeededBefore ? "update" : "first",
      started_at: new Date(now).toISOString(),
      finished_at: null,
      error_code: null,
      startedMs: now,
      succeededBefore: g.succeededBefore === true,
    });
    return json(res, 202, graphWire(graphs.get(pid)));
  }

  // Plan (`/plan`, `/plan/tasks`, `/plan/preview`): one run state per project. A run needs a succeeded graph build (else 409
  // no_graph_build); it is resolved when it is read, its ten steps spread over `planMs`, and then the project has the fixed plan below.
  const PLAN_STEPS = ["snapshot", "partition", "describe", "project", "tasks", "stitch", "review", "diff", "persist", "check"];
  const STUB_PARTS = 8;
  const plans = new Map(); // pid -> {status, run_id, replan, startedMs, has_plan}
  let planSeq = 0;
  const planOf = (pid) => {
    const p = plans.get(pid) ?? { status: "idle", run_id: null, replan: null, startedMs: 0, has_plan: false };
    if (p.status === "running" && Date.now() - p.startedMs >= state.planMs) {
      p.status = "succeeded";
      p.has_plan = true;
    }
    plans.set(pid, p);
    return p;
  };
  const planWire = (p) => {
    const done =
      p.status === "running"
        ? Math.min(PLAN_STEPS.length - 1, Math.floor(((Date.now() - p.startedMs) / state.planMs) * PLAN_STEPS.length))
        : PLAN_STEPS.length;
    const started = p.run_id === null ? null : new Date(p.startedMs).toISOString();
    const step = p.run_id === null ? null : PLAN_STEPS[Math.min(done, PLAN_STEPS.length - 1)];
    // The tasks step reports its planning parts, as the API does (8 parts here, spread over the step).
    const elapsedSteps = p.run_id === null ? 0 : ((Date.now() - p.startedMs) / state.planMs) * PLAN_STEPS.length;
    const partsDone = Math.min(STUB_PARTS, Math.max(0, Math.floor((elapsedSteps - done) * STUB_PARTS)));
    const items = p.status === "running" && step === "tasks" ? { done: partsDone, total: STUB_PARTS } : null;
    return {
      status: p.status,
      has_plan: p.has_plan,
      run_id: p.run_id,
      mode: p.run_id === null ? null : p.replan ? "update" : "first",
      replan: p.replan,
      step,
      steps_done: p.run_id === null ? 0 : done,
      steps_total: p.run_id === null ? 0 : PLAN_STEPS.length,
      step_items_done: items ? items.done : null,
      step_items_total: items ? items.total : null,
      started_at: started,
      finished_at: p.status === "running" || p.run_id === null ? null : new Date(p.startedMs + state.planMs).toISOString(),
      unchanged: p.status === "running" || p.run_id === null ? null : false,
      error_code: null,
    };
  };
  const stubTask = (id, phase, title, status, waits, implementsName, over = {}) => ({
    task_id: id,
    area_id: "AREA::CHECKOUT",
    story_id: "STORY::CHECKOUT",
    phase,
    title,
    description: `${title}.`,
    done_when: [{ text: `${title} works.`, source: "spec" }],
    complexity: "medium",
    status,
    implements: [{ spec_id: "FEAT::CHECKOUT", label: "Feature", name: implementsName }],
    waits_for: waits,
    ready: status === "pending" && waits.length === 0,
    revision: 1,
    commit: null,
    block_reason: null,
    has_open_questions: false,
    planned_by: "model",
    ...over,
  });
  const stubStory = (id, areaId, title, progress) => ({
    story_id: id,
    area_id: areaId,
    kind: "feature",
    title,
    summary: null,
    progress,
    status: "active",
  });
  // A plan of a run that partly fell back: one rule-made task in the project-wide area, one blocking review finding.
  const PLAN_CONTENT = {
    plan_id: "plan-stub",
    last_build_id: "gb-1",
    updated_at: "2026-11-05T09:00:00Z",
    areas: [
      {
        area_id: "AREA::CHECKOUT",
        kind: "module",
        title: "Checkout",
        description: "Orders and payment.",
        status: "active",
        planned_by: "model",
        not_buildable: [{ spec_id: "REQ::RELEASE", name: "Release sign-off", reason: "release_gate", note: "Signed off by the owner." }],
      },
      {
        area_id: "AREA::PROJECT",
        kind: "project",
        title: null,
        description: null,
        status: "active",
        planned_by: "rules",
        not_buildable: [],
      },
    ],
    stories: [
      stubStory("STORY::CHECKOUT", "AREA::CHECKOUT", "Checkout", "in_progress"),
      stubStory("STORY::CHECKOUT::TRACK", "AREA::CHECKOUT", "Track the order", "pending"),
      stubStory("STORY::PROJECT::OPS", "AREA::PROJECT", "Operations", "pending"),
    ],
    tasks: [
      stubTask("TASK::SETUP", "setup", "Set up the checkout module", "completed", [], "Checkout"),
      stubTask("TASK::ORDER_API", "contract", "Define the order API", "in_progress", ["TASK::SETUP"], "Checkout"),
      stubTask("TASK::PAY", "logic", "Take the payment", "pending", ["TASK::ORDER_API"], "Checkout"),
      stubTask("TASK::TRACK", "logic", "Show the order status", "pending", ["TASK::PAY"], "Checkout", {
        story_id: "STORY::CHECKOUT::TRACK",
      }),
      stubTask("TASK::RETRY", "quality", "Quality: Retry model calls", "pending", [], "Checkout", {
        area_id: "AREA::PROJECT",
        story_id: "STORY::PROJECT::OPS",
        planned_by: "rules",
      }),
    ],
    quality: {
      run_id: "plan-run-stub",
      status: "partially_succeeded",
      finished_at: "2026-11-05T09:00:00Z",
      fallbacks: { tasks: 1 },
      tasks_planned: 5,
      review: { blocking: 1 },
      review_failed: false,
      unplaced: 0,
      oversized: 0,
    },
    review: [{ area_id: "AREA::CHECKOUT", task_id: null, problem: "No task covers refunds.", severity: "blocking" }],
  };
  const validPlanBody = (text) => {
    if (text.trim() === "") return true;
    try {
      const b = JSON.parse(text);
      return (
        typeof b === "object" &&
        b !== null &&
        !Array.isArray(b) &&
        Object.keys(b).every((k) => k === "replan") &&
        (b.replan === undefined || typeof b.replan === "boolean")
      );
    } catch {
      return false;
    }
  };

  async function planRoute({ req, res, pid, did, entry }) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const built = graphOf(pid).succeededBefore === true;
    if (did === "preview" && req.method === "GET") {
      const features = built
        ? [
            { spec_id: "FEAT::CHECKOUT", name: "Checkout" },
            { spec_id: "FEAT::SEARCH", name: "Search" },
          ]
        : [];
      return json(res, 200, {
        build_id: built ? "gb-1" : null,
        mode: built ? "first" : null,
        unchanged: built ? false : null,
        calls: built ? 12 : null,
        input_tokens: built ? 48000 : null,
        features,
      });
    }
    if (did === "tasks" && req.method === "GET") return planOf(pid).has_plan ? json(res, 200, PLAN_CONTENT) : err(res, 404, "no_plan");
    if (did) return err(res, 404, "not_found");
    if (req.method === "GET") return json(res, 200, planWire(planOf(pid)));
    if (req.method !== "POST") return err(res, 405, "method_not_allowed");
    const text = Buffer.concat(chunks).toString();
    if (!validPlanBody(text)) return err(res, 422, "validation_failed");
    entry.bodyText = text;
    if (graphOf(pid).status === "running") return err(res, 409, "graph_build_running");
    if (!built) return err(res, 409, "no_graph_build");
    const p = planOf(pid);
    if (p.status === "running") return json(res, 202, planWire(p));
    plans.set(pid, {
      status: "running",
      run_id: `run-${String(++planSeq)}`,
      replan: text.includes("true"),
      startedMs: Date.now(),
      has_plan: p.has_plan,
    });
    return json(res, 202, planWire(plans.get(pid)));
  }

  // Findings and claims (`/findings`, `/plan/claims`): per project, seeded through the control port (`seedWork`). A resolve or dismiss of a
  // blocking finding answers `unblocked: 1`; a closed or unknown finding answers the API's 409 `finding_closed` / 404 `finding_not_found`;
  // `state.workFail` (an API code) makes the next write fail with 503, `state.workReadFail` every read until it is cleared (the error states). A forced release frees the story claim, as the API's.
  const works = new Map(); // pid -> {findings: [...], claims: [...]}
  const workOf = (pid) => works.get(pid) ?? works.set(pid, { findings: [], claims: [] }).get(pid);
  const person = (name, you = false, agentLabel = null) => ({ name, you, agent_label: agentLabel });
  const closed = (f, by, note) => ({
    ...f,
    status: by.status,
    resolved_by: person("Alice", true),
    resolved_at: new Date().toISOString(),
    resolution_note: note,
  });
  async function workRoute({ req, res, url, pid, b, did, action, rest }) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const work = workOf(pid);
    const write = req.method === "POST";
    if (!write && state.workReadFail) return err(res, 503, state.workReadFail);
    if (write && state.workFail) {
      const code = state.workFail;
      state.workFail = null;
      return err(res, 503, code);
    }
    if (b === "findings" && !did && req.method === "GET") {
      const status = url.searchParams.get("status") ?? "open";
      const node = url.searchParams.get("node");
      const rows = work.findings.filter((f) => (status === "all" || f.status === status) && (!node || f.about.includes(node)));
      rows.sort((x, y) => Number(y.blocking) - Number(x.blocking) || y.created_at.localeCompare(x.created_at));
      return json(res, 200, { findings: rows, next: null });
    }
    if (b === "findings" && did && (action === "resolve" || action === "dismiss") && write) {
      const f = work.findings.find((x) => x.finding_id === decodeURIComponent(did));
      if (!f) return err(res, 404, "finding_not_found");
      if (f.status !== "open") return err(res, 409, "finding_closed");
      const note = JSON.parse(Buffer.concat(chunks).toString() || "{}").note ?? null;
      Object.assign(f, closed(f, { status: action === "resolve" ? "resolved" : "dismissed" }, note));
      return json(res, 200, { finding: f, unblocked: f.blocking ? 1 : 0 });
    }
    if (b === "plan" && did === "claims" && !action && req.method === "GET") {
      return planOf(pid).has_plan ? json(res, 200, { claims: work.claims }) : err(res, 404, "no_plan");
    }
    if (b === "plan" && did === "claims" && rest === "release" && write) {
      const before = work.claims.length;
      work.claims = work.claims.filter((c) => c.story_id !== decodeURIComponent(action));
      return json(res, 200, { released: before === work.claims.length ? 0 : 2 });
    }
    return err(res, 404, "not_found");
  }

  // Gaps (FR-GWEB-04, FR-GWEB-06): per project, seeded through the control port (`seedGaps`: full `GapView` rows plus a stub-only
  // `stories` id list for the story filter and `old_ids`, the old `FND::` ids of a migrated finding that `GET /gaps/{id}` resolves to the
  // gap, as the API does; an `ABOUT` / `IMPACTS` node may carry `aliases`, its other spec ids), the latest `GapRunStatus` and the noise
  // rules. Reads: `GET /gaps` filters as `list_gaps` does (status default open, answered, applying; kind, severity, origin, route repeat;
  // story_id; assigned_to `me`, `none` or a user id; limit, 50 by default), sorts by severity, reach (tasks), then newest, and drops
  // options, history and reach from the items; `cursor` is an offset. Every gap has `assigned_to` (null unless seeded or assigned). Writes, in memory, with the API's
  // checks and codes: `edits_preview` (edits made from the option or the own text: `amend` after the first witness line, or a
  // clarifications-document section; own text containing "FALLBACK" falls back to the document), `answer` (409 gap_stale unless
  // `updated_at` is the gap's; the gap moves to answered with a pending clarification by the caller and a new `updated_at`: the answer
  // is staged, `apply_at` null; a development gap that blocks applies every staged answer at once, `immediate`), `dismiss` (204, an
  // open gap only), raise (`POST /gaps`, 201), `apply` ("Resolve gaps": answered -> applying; 409 gap_wrong_state when nothing is
  // staged), `ai_answers`, revert (an applied answer of a
  // resolved gap opens a follow-up gap, linked both ways in `history.follow_ups`; a pending one sends the gap back open), the noise rules
  // and `PUT /gaps/{id}/assignee` (a member of the project or null; 400 assignee_not_member, 409 gap_wrong_state for a resolved gap; an
  // `assigned` or `unassigned` event by the caller). `settleGaps` (control) settles the
  // applying gaps: resolved, clarification applied. `recordGaps` (control) is a running gap run recording one more site: its gaps join
  // the list (a gap with the same id is replaced) and a running run's `step_items_done` counts the site. `state.gapFail` (an API code) makes every gap route answer 503 with it. An unseeded
  // project has no gaps and an idle run.
  const gapSets = new Map(); // pid -> {gaps, run, rules, reverted: AiAnswer[], seq}
  const gapsOf = (pid) =>
    gapSets.get(pid) ?? gapSets.set(pid, { gaps: [], run: { status: "idle", result: null }, rules: [], reverted: [], seq: 0 }).get(pid);
  /**
   * The gap run a finished build queues, as the API's `after_build` does (a run that was never made only: a seeded run is the truth). It is
   * queued at once, runs for the second half of `state.gapMs` and then completes with no gap of its own; it is resolved when it is read
   * (`settleStubRun`), like the build.
   */
  function queueGapRun(pid) {
    const set = gapsOf(pid);
    if (set.run.status !== "idle") return;
    set.run = {
      status: "queued",
      run_id: "run-stub",
      mode: "full",
      step: null,
      steps_done: 0,
      steps_total: 7,
      step_items_done: null,
      step_items_total: null,
      started_at: new Date().toISOString(),
      result: null,
    };
    set.stubRunMs = Date.now();
  }
  function settleStubRun(set) {
    if (set.stubRunMs === undefined || set.run.status === "completed") return;
    const spent = Date.now() - set.stubRunMs;
    if (spent >= state.gapMs) {
      set.run = {
        ...set.run,
        status: "completed",
        step: "finish",
        steps_done: 7,
        finished_at: new Date().toISOString(),
        result: {
          run_id: set.run.run_id,
          mode: "full",
          build_id: null,
          status: "completed",
          counts: {},
          cost: {},
          noise: { sections: [] },
          deferred_sites: [],
          held_tasks: [],
          warnings: [],
        },
      };
    } else if (spent >= state.gapMs / 2) set.run = { ...set.run, status: "running", step: "investigate", steps_done: 3 };
  }
  const SEVERITY_RANK = { blocker: 0, major: 1, minor: 2 };
  const STUB_FIELDS = new Set(["stories", "old_ids"]);
  const publicGap = (g, full) => {
    const view = { assigned_to: null, ...Object.fromEntries(Object.entries(g).filter(([k]) => !STUB_FIELDS.has(k))) };
    return full ? { history: { events: [], follow_ups: [] }, ...view } : { ...view, options: [], history: null, reach: null };
  };
  let clock = Date.now();
  /** A timestamp strictly later than the previous one (an answer always changes `updated_at`). */
  const tick = () => {
    clock = Math.max(clock + 1, Date.now());
    return new Date(clock).toISOString();
  };
  const invalid = (res, field, reason) => err(res, 400, "answer_invalid", { field, reason });
  const wrongState = (res, status, allowed) => err(res, 409, "gap_wrong_state", { status, allowed });
  const historyOf = (g) => (g.history ??= { events: [], follow_ups: [] });
  const event = (g, name, detail = {}, clrId = null, actor = "u1") =>
    historyOf(g).events.push({ gap_id: g.gap_id, clr_id: clrId, at: tick(), actor, event: name, detail });
  const textLength = (v, min, max) => typeof v === "string" && v.trim().length >= min && v.length <= max;
  async function readJsonBody(req) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const text = Buffer.concat(chunks).toString();
    if (text.trim() === "") return {};
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }
  /** The edits and preview sections of an answer (see the header). */
  function draftEdits(g, text, placement, fallback) {
    const w = (g.witness ?? [])[0];
    const amend = placement === "amend" || (placement === undefined && w !== undefined && !fallback);
    if (amend && w !== undefined && !fallback) {
      return {
        placement: "amend",
        edits: [{ placement: "amend", target: { fragment_id: w.fragment_id, anchor_quote: w.quote }, operation: "insert_after", text }],
        sections: [
          {
            fragment_id: w.fragment_id,
            lineage: w.document_id ?? null,
            heading_path: w.heading_path ?? [],
            before: w.quote,
            after: `${w.quote}\n${text}\n> Clarified by ${g.gap_id}`,
          },
        ],
        fallback: null,
      };
    }
    return {
      placement: "document",
      edits: [{ placement: "document", target: { heading: g.title }, operation: "append_section", text }],
      sections: [{ fragment_id: null, lineage: null, heading_path: [`${g.gap_id} ${g.title}`], before: "", after: text }],
      fallback: fallback ? "edits_failed" : null,
    };
  }
  /** Checks an answer or preview body against the gap; returns the error responder or the drafted edits. */
  function checkAnswer(g, body) {
    if (!["open", "answered"].includes(g.status)) return (res) => wrongState(res, g.status, ["open", "answered"]);
    const hasRank = body.option_rank !== undefined && body.option_rank !== null;
    const hasText = body.own_text !== undefined && body.own_text !== null;
    if (hasRank === hasText) return (res) => invalid(res, "answer", "one_of_option_or_text");
    if (body.placement !== undefined && body.placement !== null && !["amend", "document"].includes(body.placement))
      return (res) => err(res, 422, "validation_failed");
    if (g.kind === "contradiction" && body.placement === "document") return (res) => invalid(res, "placement", "contradiction_amends");
    if (hasText && !textLength(body.own_text, 1, 4000)) return (res) => invalid(res, "own_text", "length");
    const option = hasRank ? (g.options ?? []).find((o) => o.rank === body.option_rank) : null;
    if (hasRank && !option) return (res) => invalid(res, "option_rank", "unknown");
    const text = hasText ? body.own_text.trim() : option.detail || option.label;
    const fallback = hasText && text.includes("FALLBACK");
    const placement = g.kind === "contradiction" ? "amend" : (body.placement ?? undefined);
    return { draft: draftEdits(g, text, placement, fallback), option, text };
  }
  function listGaps(set, q) {
    const many = (name, fallback) => (q.getAll(name).length ? q.getAll(name) : fallback);
    const statuses = many("status", ["open", "answered", "applying"]);
    const keep = (name, value) => many(name, null) === null || many(name, null).includes(value);
    const story = q.get("story_id");
    const assigned = q.get("assigned_to");
    const assignee = assigned === "me" ? ME.id : assigned;
    const reach = (g) => (g.reach?.tasks ?? []).length;
    return set.gaps
      .filter(
        (g) =>
          statuses.includes(g.status) &&
          keep("kind", g.kind) &&
          keep("severity", g.severity) &&
          keep("origin", g.origin) &&
          keep("route", g.route) &&
          (!story || (g.stories ?? []).includes(story)) &&
          (assigned === null || (assigned === "none" ? !g.assigned_to : g.assigned_to === assignee)),
      )
      .sort(
        (x, y) =>
          SEVERITY_RANK[x.severity] - SEVERITY_RANK[y.severity] ||
          reach(y) - reach(x) ||
          y.created_at.localeCompare(x.created_at) ||
          x.gap_id.localeCompare(y.gap_id),
      );
  }
  function aiAnswers(set) {
    const live = set.gaps
      .filter(
        (g) =>
          g.clarification && g.clarification.status !== "reverted" && (g.clarification.mode === "ai" || g.clarification.mode === "pointer"),
      )
      .map((g) => ({
        gap_id: g.gap_id,
        title: g.title,
        question: g.question,
        gap_status: g.status,
        clarification: g.clarification,
        follow_up: null,
      }));
    return [...live, ...set.reverted].sort((x, y) => y.clarification.answered_at.localeCompare(x.clarification.answered_at));
  }
  function revert(set, clrId, reason, res) {
    const g = set.gaps.find((x) => x.clarification?.clr_id === clrId);
    if (!g) return err(res, 404, "gap_not_found");
    if (!textLength(reason, 1, 500)) return invalid(res, "reason", "length");
    const c = g.clarification;
    const resolved = g.status === "resolved" && (c.status === "applied" || c.status === "absorbed");
    const pending = ["answered", "applying"].includes(g.status) && c.status === "pending";
    if (!resolved && !pending) return wrongState(res, g.status, ["answered", "applying", "resolved"]);
    const reverted = { ...c, status: "reverted" };
    const built = resolved ? `gb-revert-${String(++set.seq)}` : null;
    event(g, "reverted", { reason, mode: c.mode }, clrId);
    if (resolved) {
      const id = `${g.gap_id}-R${String(++set.seq)}`;
      const now = tick();
      const follow = {
        ...structuredClone(g),
        gap_id: id,
        status: "open",
        route: "ask",
        resolution: null,
        resolved_at: null,
        clarification: null,
        created_by: "u1",
        created_at: now,
        updated_at: now,
        signature: `${g.signature}|reverted|${clrId}`,
        history: { events: [], follow_ups: [{ gap_id: g.gap_id, reason: "reverted", direction: "continues" }] },
      };
      event(follow, "follow_up", { reason: "reverted", of: g.gap_id });
      historyOf(g).follow_ups.push({ gap_id: id, reason: "reverted", direction: "continued_by" });
      g.clarification = reverted;
      g.updated_at = tick();
      set.gaps.push(follow);
      if (c.mode === "ai" || c.mode === "pointer")
        set.reverted.push({
          gap_id: g.gap_id,
          title: g.title,
          question: g.question,
          gap_status: g.status,
          clarification: reverted,
          follow_up: id,
        });
      return json(res, 200, { clr_id: clrId, gap_id: id, follow_up: true, amendments: [], build: built });
    }
    if (c.mode === "ai" || c.mode === "pointer")
      set.reverted.push({
        gap_id: g.gap_id,
        title: g.title,
        question: g.question,
        gap_status: "open",
        clarification: reverted,
        follow_up: null,
      });
    g.status = "open";
    g.route = "ask";
    g.clarification = null;
    g.updated_at = tick();
    return json(res, 200, { clr_id: clrId, gap_id: g.gap_id, follow_up: false, amendments: [], build: null });
  }
  function raise(set, body, res) {
    if (!textLength(body.question, 10, 1000)) return invalid(res, "question", "length");
    const about = Array.isArray(body.about) ? body.about : [];
    if (about.length < 1 || about.length > 20) return invalid(res, "about", "count");
    if (about.some((id) => typeof id !== "string" || id.startsWith("UNKNOWN"))) return invalid(res, "about", "unknown");
    const question = body.question.trim();
    const same = set.gaps.find(
      (g) =>
        g.origin === "person" &&
        g.status === "open" &&
        g.question === question &&
        JSON.stringify(g.about.map((n) => n.spec_id).sort()) === JSON.stringify([...about].sort()),
    );
    if (same) return json(res, 201, { gap_id: same.gap_id, created: false });
    const id = `GAP::PERSON${String(++set.seq)}`;
    const now = tick();
    const g = {
      gap_id: id,
      kind: body.kind ?? "missing",
      severity: body.severity ?? "major",
      status: "open",
      origin: "person",
      route: "ask",
      resolution: null,
      title: question.split("\n")[0].slice(0, 160),
      question,
      why: "",
      detector: "person",
      signature: `person|${id}`,
      blocking: false,
      run_id: null,
      created_by: "u1",
      created_at: now,
      updated_at: now,
      resolved_at: null,
      about: about.map((spec_id) => ({ spec_id, label: "", name: "", aliases: [] })),
      impacts: [],
      witness: [],
      options: [],
      clarification: null,
      history: { events: [], follow_ups: [] },
      reach: { stories: [], tasks: [] },
    };
    event(g, "raised");
    set.gaps.push(g);
    return json(res, 201, { gap_id: id, created: true });
  }
  /** Every staged answer goes to one rebuild: answered -> applying; null when nothing is staged. */
  function applyStaged(set) {
    const waiting = set.gaps.filter((g) => g.status === "answered" && g.clarification?.status === "pending");
    if (waiting.length === 0) return null;
    const build = `gb-apply-${String(++set.seq)}`;
    for (const g of waiting) {
      g.status = "applying";
      g.updated_at = tick();
      g.applying_build = build;
      event(g, "applying", { build }, g.clarification.clr_id, "apply");
    }
    return { gap_ids: waiting.map((g) => g.gap_id), clr_ids: waiting.map((g) => g.clarification.clr_id), amendments: [], build };
  }
  async function gapWrite({ req, res, set, did, action, rest }) {
    const body = await readJsonBody(req);
    if (body === null || typeof body !== "object" || Array.isArray(body)) return err(res, 422, "validation_failed");
    if (!did) return raise(set, body, res);
    if (did === "apply") {
      const applied = applyStaged(set);
      return applied ? json(res, 200, applied) : wrongState(res, "nothing_pending", ["answered"]);
    }
    if (did === "noise_rules") {
      if (!["document", "section", "pattern"].includes(body.scope)) return err(res, 422, "validation_failed");
      if (!textLength(body.value, 1, 500)) return invalid(res, "value", "length");
      if (!textLength(body.reason, 1, 500)) return invalid(res, "reason", "length");
      const same = set.rules.find((r) => r.scope === body.scope && r.value === body.value);
      if (same) return json(res, 201, { rule_id: same.rule_id });
      const rule = {
        rule_id: `rule-${String(++set.seq)}`,
        scope: body.scope,
        value: body.value,
        reason: body.reason,
        created_by: "u1",
        created_at: tick(),
      };
      set.rules.push(rule);
      return json(res, 201, { rule_id: rule.rule_id });
    }
    if (did === "clarifications" && rest === "revert") return revert(set, decodeURIComponent(action), body.reason, res);
    const g = set.gaps.find((x) => x.gap_id === decodeURIComponent(did));
    if (!g) return err(res, 404, "gap_not_found");
    if (action === "dismiss") {
      if (!textLength(body.reason, 1, 500)) return invalid(res, "reason", "length");
      if (g.status !== "open") return wrongState(res, g.status, ["open"]);
      set.gaps = set.gaps.filter((x) => x !== g);
      res.writeHead(204);
      return res.end();
    }
    if (action !== "edits_preview" && action !== "answer") return err(res, 404, "not_found");
    const checked = checkAnswer(g, body);
    if (typeof checked === "function") return checked(res);
    if (action === "edits_preview") return json(res, 200, { gap_id: g.gap_id, ...checked.draft, drafted: body.own_text != null });
    if (typeof body.updated_at !== "string") return err(res, 422, "validation_failed");
    if (body.updated_at !== g.updated_at) return err(res, 409, "gap_stale");
    if (body.edits !== undefined && (!Array.isArray(body.edits) || body.edits.length < 1 || body.edits.length > 12))
      return invalid(res, "edits", "edits_count");
    const edits = body.edits ?? checked.draft.edits;
    const placement = edits[0]?.placement ?? checked.draft.placement;
    const replaced = g.clarification?.status === "pending" ? g.clarification.clr_id : null;
    if (replaced) event(g, "replaced", {}, replaced);
    const clrId = `CLR::${String(++set.seq)}`;
    g.status = "answered";
    g.route = "ask";
    g.clarification = {
      clr_id: clrId,
      mode: checked.option ? "option" : "own",
      placement,
      status: "pending",
      option_label: checked.option ? checked.option.label : null,
      text: checked.text,
      answered_by: ME.id,
      answered_at: tick(),
      applied_build_id: null,
      basis: [],
      confidence: null,
    };
    g.updated_at = tick();
    event(g, "answered", { mode: g.clarification.mode }, clrId);
    const immediate = g.origin === "development" && g.blocking === true;
    if (immediate) applyStaged(set);
    return json(res, 200, {
      gap_id: g.gap_id,
      clr_id: clrId,
      replaced,
      placement,
      amendments: edits.filter((e) => e.placement === "amend").map((_, i) => `AMD::${clrId}::${String(i + 1)}`),
      apply_at: immediate ? tick() : null,
      immediate,
    });
  }
  /** `PUT /gaps/{gap_id}/assignee` (see the header). */
  async function assignGap({ req, res, set, pid, did, action }) {
    const body = await readJsonBody(req);
    if (!did || action !== "assignee") return err(res, 405, "method_not_allowed");
    const g = set.gaps.find((x) => x.gap_id === decodeURIComponent(did));
    if (!g) return err(res, 404, "gap_not_found");
    const userId = body?.user_id;
    if (body === null || !("user_id" in body) || (userId !== null && typeof userId !== "string")) return err(res, 422, "validation_failed");
    if (g.status === "resolved") return wrongState(res, g.status, ["open", "answered", "applying"]);
    if (userId !== null && !people.list(pid).members.some((m) => m.user_id === userId)) return err(res, 400, "assignee_not_member");
    const previous = g.assigned_to ?? null;
    if (previous !== userId) {
      g.assigned_to = userId;
      if (userId === null) event(g, "unassigned", { previous }, null, ME.id);
      else event(g, "assigned", { user_id: userId, previous }, null, ME.id);
    }
    return json(res, 200, publicGap(g, true));
  }
  async function gapRoute({ req, res, url, pid, b, did, action }) {
    const rest = url.pathname.split("/").filter(Boolean)[7];
    if (state.gapFail) {
      for await (const c of req) void c;
      return err(res, 503, state.gapFail);
    }
    const set = gapsOf(pid);
    if (b === "gap") {
      for await (const c of req) void c;
      if (req.method !== "GET" || did !== "runs") return err(res, 404, "not_found");
      if (action === "latest") {
        settleStubRun(set);
        return json(res, 200, set.run);
      }
      if (rest === "noise" && set.run.result && set.run.result.run_id === decodeURIComponent(action))
        return json(res, 200, set.run.result.noise);
      return err(res, 404, "gap_run_not_found");
    }
    if (req.method === "POST") return gapWrite({ req, res, set, did, action, rest });
    if (req.method === "PUT") return assignGap({ req, res, set, pid, did, action });
    for await (const c of req) void c;
    if (req.method === "DELETE") {
      if (did !== "noise_rules" || !action) return err(res, 405, "method_not_allowed");
      const before = set.rules.length;
      set.rules = set.rules.filter((r) => r.rule_id !== decodeURIComponent(action));
      if (before === set.rules.length) return err(res, 404, "noise_rule_not_found");
      res.writeHead(204);
      return res.end();
    }
    if (req.method !== "GET") return err(res, 405, "method_not_allowed");
    if (did === "noise_rules") return json(res, 200, { rules: set.rules });
    if (did === "ai_answers") return json(res, 200, { answers: aiAnswers(set).slice(0, Number(url.searchParams.get("limit") ?? 50)) });
    if (did) {
      const id = decodeURIComponent(did);
      const g = set.gaps.find((x) => x.gap_id === id || (x.old_ids ?? []).includes(id));
      return g ? json(res, 200, publicGap(g, true)) : err(res, 404, "gap_not_found");
    }
    const q = url.searchParams;
    const rows = listGaps(set, q);
    const limit = Number(q.get("limit") ?? 50);
    const from = Number(q.get("cursor") ?? 0);
    if (!Number.isInteger(from) || from < 0) return err(res, 400, "invalid_cursor");
    const page = rows.slice(from, from + limit);
    return json(res, 200, { gaps: page.map((g) => publicGap(g, false)), next: from + limit < rows.length ? String(from + limit) : null });
  }
  /** Test control: the update build of the applied answers finished; every applying gap resolves with its clarification applied. */
  function settleGaps(pid) {
    const set = gapsOf(pid);
    for (const g of set.gaps.filter((x) => x.status === "applying")) {
      g.status = "resolved";
      g.resolution = g.clarification?.mode === "ai" ? "ai_answered" : "answered";
      g.resolved_at = tick();
      g.updated_at = g.resolved_at;
      if (g.clarification) {
        g.clarification = { ...g.clarification, status: "applied", applied_build_id: g.applying_build ?? "gb-apply" };
        event(g, "reconciled", { outcome: "applied" }, g.clarification.clr_id, "reconcile");
      }
      delete g.applying_build;
    }
  }

  /**
   * The source an import of a Confluence link or a Jira epic makes (`kind` confluence, no folder): it lists, then imports its pages for `parseMs`, then is done.
   * The card is the same summary the unified status gives.
   */
  function importedSource(pid, { kind, name, link, pages }) {
    const row = {
      id: crypto.randomUUID(),
      kind,
      folder_id: null,
      link,
      name,
      state: "listing",
      failure_reason: null,
      connected_by: "11111111-1111-4111-8111-111111111111",
      created_at: new Date().toISOString(),
      last_synced_at: null,
      progress: { listing_finished: false, total: 0, finished: 0 },
    };
    sourcesOf(pid).push(model.stamp(row));
    setTimeout(() => {
      row.state = "importing";
      row.progress = { listing_finished: true, total: pages, finished: 1 };
    }, 300);
    if (Number.isFinite(state.parseMs)) {
      setTimeout(() => {
        row.state = "done";
        row.progress = { listing_finished: true, total: pages, finished: pages };
      }, state.parseMs);
    }
    return model.summaries(pid).find((s) => s.id === row.id);
  }

  async function sourcesRoute({ req, res, url, pid, did, action, entry }) {
    const drain = async () => {
      for await (const c of req) void c;
    };
    const readJson = async () => {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      try {
        return JSON.parse(Buffer.concat(chunks).toString() || "{}");
      } catch {
        return null;
      }
    };
    try {
      if (!did && req.method === "GET" && state.sourcesFail) {
        await drain();
        return err(res, 503, state.sourcesFail); // the status read of the project cannot be answered
      }
      if (!did && req.method === "GET") {
        await drain();
        return json(res, 200, url.searchParams.get("batches") === "1" ? model.status(pid) : progressOf(pid));
      }
      if (did === "uploads" && req.method === "POST" && !action) {
        const body = await readJson();
        const b = model.start(pid, body?.expected_files, body?.name);
        return json(res, 201, { source: model.summaries(pid).find((s) => s.id === b.id) });
      }
      if (did === "confluence" && req.method === "POST" && (action === undefined || action === "check")) {
        const body = await readJson();
        const checked =
          typeof body?.link === "string" ? checkConfluence(body.link, body.follow === true) : { status: 422, code: "validation_failed" };
        if (!("body" in checked)) return err(res, checked.status, checked.code);
        if (action === "check") return json(res, 200, checked.body);
        return json(res, 201, {
          source: importedSource(pid, { kind: "confluence", name: checked.body.title, link: body.link, pages: checked.body.pages }),
        });
      }
      if (did === "notion" && req.method === "POST" && (action === undefined || action === "check")) {
        const body = await readJson();
        const checked =
          typeof body?.link === "string"
            ? (bindingRefusal(integrations.notionHealth(pid)) ?? checkNotion(body.link))
            : { status: 422, code: "validation_failed" };
        if (!("body" in checked)) return err(res, checked.status, checked.code);
        if (action === "check") return json(res, 200, checked.body);
        return json(res, 201, {
          source: importedSource(pid, { kind: "notion", name: checked.body.title, link: body.link, pages: checked.body.pages }),
        });
      }
      if (did === "jira-epic" && req.method === "POST" && (action === undefined || action === "check")) {
        const body = await readJson();
        const checked =
          typeof body?.key_or_link === "string" ? checkJiraEpic(body.key_or_link) : { status: 422, code: "validation_failed" };
        if (!("body" in checked)) return err(res, checked.status, checked.code);
        if (action === "check") return json(res, 200, checked.body);
        const { key, summary, issues, children } = checked.body;
        const exclude = body.exclude ?? [];
        const known = new Set(children.map((c) => c.key));
        if (!Array.isArray(exclude) || !exclude.every((k) => typeof k === "string" && known.has(k)))
          return err(res, 422, "validation_failed");
        const link = `https://${SITE_HOST}/browse/${key}`;
        return json(res, 201, {
          source: importedSource(pid, {
            kind: "jira_epic",
            name: `${key}: ${summary}`,
            link,
            pages: Math.min(issues, 200) - new Set(exclude).size,
          }),
        });
      }
      if (did === "linear" && req.method === "POST" && (action === undefined || action === "check")) {
        const body = await readJson();
        const checked = typeof body?.link === "string" ? checkLinearSource(body.link) : { status: 422, code: "validation_failed" };
        if (!("body" in checked)) return err(res, checked.status, checked.code);
        if (action === "check") return json(res, 200, checked.body);
        const { kind, name, issues, items, documents } = checked.body;
        const exclude = body.exclude ?? [];
        const known = new Set(items.map((item) => item.identifier));
        if (!Array.isArray(exclude) || !exclude.every((id) => typeof id === "string" && known.has(id)))
          return err(res, 422, "validation_failed");
        const taken = kind === "issue" ? 1 : 0;
        return json(res, 201, {
          source: importedSource(pid, {
            kind: "linear_project",
            name,
            link: checked.link,
            pages: issues - new Set(exclude).size + documents + taken,
          }),
        });
      }
      if (
        did &&
        did !== "gdrive" &&
        did !== "uploads" &&
        did !== "confluence" &&
        did !== "jira-epic" &&
        did !== "linear" &&
        did !== "notion"
      ) {
        if (!action && req.method === "DELETE") {
          await drain();
          model.remove(pid, did);
          res.writeHead(204);
          return res.end();
        }
        if (!action && req.method === "PATCH") {
          const body = await readJson();
          return json(res, 200, model.rename(pid, did, body?.name));
        }
        if (action === "files" && req.method === "GET") {
          await drain();
          const limit = url.searchParams.has("limit") ? Number(url.searchParams.get("limit")) : 200;
          return json(res, 200, model.page(pid, did, limit, url.searchParams.get("cursor")));
        }
        if (action === "files" && req.method === "POST") return await addToBatch({ req, res, url, pid, did, entry });
        if (action === "end" && req.method === "POST") {
          await drain();
          const source = model.end(pid, did);
          if (!source) {
            res.writeHead(204);
            return res.end();
          }
          return json(res, 200, { source });
        }
      }
    } catch (e) {
      if (e instanceof StubError) {
        await drain().catch(() => undefined);
        return err(res, e.status, e.code);
      }
      throw e;
    }
    if (did !== "gdrive" || req.method !== "POST" || (action !== undefined && action !== "test")) {
      await drain();
      return err(res, 404, "not_found");
    }
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const sent = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    const link = sent.link;
    if (typeof link !== "string") return err(res, 422, "validation_failed");
    const testing = action === "test";
    const fail = (reason) => (testing ? json(res, 200, { result: "failed", reason }) : err(res, REASON_HTTP[reason], reason));
    const id = FOLDER_LINK.exec(link)?.[1];
    if (!id) return fail("invalid_link");
    if (sourcesOf(pid).some((c) => c.folder_id === id)) return fail("already_connected");
    const reserved = /^fake-reason-(.+)$/.exec(id)?.[1];
    if (reserved && reserved in REASON_HTTP) return fail(reserved);
    if (id !== "fake-synthetic-30" && id !== "fake-synthetic-23" && !(id in SAMPLE) && !id.startsWith("fake-import-"))
      return fail("no_access");
    const shape = SAMPLE[id] ?? SAMPLE.default;
    if (testing) {
      return json(res, 200, {
        result: "ok",
        folder: { id, name: "Recorded folder" },
        folder_count: shape.folder_count,
        file_count: shape.categories.reduce((n, c) => n + c[2], 0),
        categories: shape.categories.map(([category, processing_class, count]) => ({ category, processing_class, count })),
        files: driveFilesOf(id),
        files_capped: driveFilesCapped(id),
      });
    }
    // `exclude`: ids of listed files not to import; anything else is a 422.
    const listed = driveFilesOf(id);
    const exclude = sent.exclude ?? [];
    const known = new Set(listed.map((f) => f.id));
    if (!Array.isArray(exclude) || !exclude.every((x) => typeof x === "string" && known.has(x))) return err(res, 422, "validation_failed");
    const skipped = new Set(shape.categories.filter((c) => c[1] === "skipped").map((c) => c[0]));
    const source = {
      id: crypto.randomUUID(),
      kind: "gdrive",
      folder_id: id,
      link: `https://drive.google.com/drive/folders/${id}`,
      name: "Recorded folder",
      state: "listing",
      failure_reason: null,
      connected_by: "11111111-1111-4111-8111-111111111111",
      created_at: new Date().toISOString(),
      last_synced_at: null,
      progress: { listing_finished: false, total: 0, finished: 0 },
    };
    // The import has not listed anything yet, so `total` stays 0; the files it will take (the listed ones, less the skipped categories and `exclude`) are kept on the row for a test to read (`sources`).
    Object.defineProperty(source, "_imports", { value: listed.filter((f) => !skipped.has(f.category) && !exclude.includes(f.id)) });
    sourcesOf(pid).push(model.stamp(source));
    return json(res, 201, { source });
  }

  const LIST_FIELDS = ["id", "original_filename", "size_bytes", "status", "error", "summarization_status", "created_at"];
  const publicDoc = (d) => Object.fromEntries(LIST_FIELDS.map((k) => [k, d.row[k]]));
  const later = (ms, fn) => {
    if (Number.isFinite(ms)) setTimeout(fn, ms);
  };
  // K5: the parse the server starts after an upload (or a retried upload, or POST .../parse).
  const startParse = (d) => {
    d.row.status = "processing";
    d.row.error = null;
    later(state.parseMs, () => {
      d.row.status = state.parseOutcome;
      if (state.parseOutcome === "failed") d.row.error = "pdf_parse_failed";
      else d.row.markdown = `# ${d.row.original_filename}\n\nSynthetic markdown for the stub.\n`;
    });
  };

  /** Reads an upload body (raw PDF) and applies the checks of `upload_document`; `error` = [status, code]. */
  async function receiveFile({ req, url, entry }) {
    entry.contentType = req.headers["content-type"];
    const filename = url.searchParams.get("filename");
    const body = await readBody(req);
    Object.assign(entry, body);
    if (!filename) return { error: [400, "missing_filename"] };
    const spec = UPLOAD_TABLE[extensionOf(filename)];
    if (!spec || req.headers["content-type"] !== spec.type || (spec.magic && body.bytes && !body.head.startsWith(spec.magic)))
      return { error: [415, "unsupported_format"] };
    if (body.bytes === 0) return { error: [400, "empty_file"] };
    if (body.bytes > spec.cap) return { error: [413, "file_too_large", { max_bytes: spec.cap }] };
    return { filename, body };
  }
  const makeDoc = (pid, batchId, { filename, body }) => {
    const d = {
      pid,
      batchId,
      sha256: body.sha256,
      row: {
        id: crypto.randomUUID(),
        original_filename: filename,
        size_bytes: body.bytes,
        status: "processing", // K5 (GDRIVE plan 04): the server starts the parse itself
        error: null,
        summarization_status: "none",
        created_at: new Date().toISOString(),
        markdown: null,
        markdown_truncated: false,
        summarization: null,
        summarization_model: null,
        summarization_error: null,
        summarized_at: null,
      },
    };
    docs.set(d.row.id, d);
    startParse(d);
    return d;
  };
  /** POST /projects/{pid}/sources/{sid}/files (INT-SRC-0004). The batch is checked before the file. */
  async function addToBatch({ req, res, url, pid, did, entry }) {
    const up = await receiveFile({ req, url, entry });
    const summary = model.summaries(pid).find((s) => s.id === did);
    if (!summary) return err(res, 404, "source_not_found");
    if (summary.kind !== "upload" || !summary.open) return err(res, 409, "batch_ended");
    if (up.error) return err(res, ...up.error);
    const r = model.add(
      pid,
      did,
      { filename: up.filename, sha256: up.body.sha256 },
      { makeDoc: (bid) => makeDoc(pid, bid, up), reparse: startParse },
    );
    return json(res, r.outcome === "added" ? 201 : 200, { outcome: r.outcome, file: r.file, holder: r.holder });
  }

  /** `/projects/{pid}/members[/{user_id}]`, `/invites/{invite_id}` and `/leave` (api.md#routes). */
  async function membersRoute({ req, res, pid, b, did }) {
    const m = req.method;
    if (b === "members" && !did && m === "GET") return json(res, 200, people.list(pid));
    if (b === "members" && !did && m === "POST") return json(res, 201, people.add(pid, await readJson(req)));
    if (b === "members" && did && m === "DELETE") return json(res, 200, people.remove(pid, did));
    if (b === "invites" && did && m === "DELETE") {
      people.cancel(pid, did);
      res.writeHead(204);
      return res.end();
    }
    if (b === "leave" && !did && m === "POST") return json(res, 200, people.leave(pid));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/projects/{pid}/jira` and `/projects/{pid}/jira/access-check` (api.md#web-routes). */
  async function jiraRoute({ req, res, pid, did }) {
    const m = req.method;
    if (!did && m === "GET") return json(res, 200, jira.get(pid));
    if (!did && m === "PUT") return json(res, 200, jira.put(pid, await readJson(req)));
    if (did === "access-check" && m === "POST") return json(res, 200, jira.check(pid, await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/projects/{pid}/linear-gaps` and `/projects/{pid}/linear-gaps/access-check` (docs/linear/gaps.md). */
  async function linearGapsRoute({ req, res, pid, did }) {
    people.list(pid); // a non-member is a 404
    if (!did && req.method === "GET") return json(res, 200, linearGaps.get(pid));
    if (!did && req.method === "PUT") return json(res, 200, linearGaps.put(pid, await readJson(req)));
    if (did === "access-check" && req.method === "POST") return json(res, 200, linearGaps.check(pid, await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/projects/{pid}/jira-gaps` and `/projects/{pid}/jira-gaps/access-check` (docs/jira-sync/gaps.md). */
  async function jiraGapsRoute({ req, res, pid, did }) {
    const m = req.method;
    people.list(pid); // a non-member is a 404
    if (!did && m === "GET") return json(res, 200, jiraGaps.get(pid));
    if (!did && m === "PUT") return json(res, 200, jiraGaps.put(pid, await readJson(req)));
    if (did === "access-check" && m === "POST") return json(res, 200, jiraGaps.check(pid, await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/projects/{pid}/github-projects[/projects|/access-check]` (docs/github/projects.md): the destination, its projects, the check. */
  async function githubProjectsRoute({ req, res, url, pid, did }) {
    const m = req.method;
    people.list(pid); // a non-member is a 404
    if (!did && m === "GET") return json(res, 200, githubProjects.get(pid));
    if (!did && m === "PUT") return json(res, 200, githubProjects.put(pid, await readJson(req)));
    if (did === "projects" && m === "GET") return json(res, 200, githubProjects.projects(pid, url.searchParams.get("repository_id") ?? ""));
    if (did === "access-check" && m === "POST") return json(res, 200, githubProjects.check(pid, await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/projects/{pid}/notion-plan[/access-check]` (docs/notion/plan.md): the destination and the check. */
  async function notionPlanRoute({ req, res, pid, did }) {
    const m = req.method;
    people.list(pid); // a non-member is a 404
    if (!did && m === "GET") return json(res, 200, notionPlan.get(pid));
    if (!did && m === "PUT") return json(res, 200, notionPlan.put(pid, await readJson(req)));
    if (did === "access-check" && m === "POST") return json(res, 200, notionPlan.check(pid, await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/integrations/notion[/authorize|/callback]` (docs/notion/oauth.md "Routes"): the caller's Notion connection. */
  async function notionRoute({ req, res, action }) {
    const m = req.method;
    if (!action && m === "GET") return json(res, 200, notion.connection());
    if (!action && m === "DELETE") {
      notion.disconnect();
      res.writeHead(204);
      return res.end();
    }
    if (action === "authorize" && m === "POST") return json(res, 200, notion.authorize(await readJson(req)));
    if (action === "callback" && m === "POST") return json(res, 200, notion.callback(await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/projects/{pid}/linear-plan[/teams|/projects|/access-check]` (plans/LINEAR/LINEAR-PLAN.md, S3): the destination, its teams and projects, the check. */
  async function linearPlanRoute({ req, res, url, pid, did }) {
    const m = req.method;
    people.list(pid); // a non-member is a 404
    if (!did && m === "GET") return json(res, 200, linearPlan.get(pid));
    if (!did && m === "PUT") return json(res, 200, linearPlan.put(pid, await readJson(req)));
    if (did === "teams" && m === "GET") return json(res, 200, linearPlan.teams(pid));
    if (did === "projects" && m === "GET") return json(res, 200, linearPlan.projects(pid, url.searchParams.get("team_id") ?? ""));
    if (did === "access-check" && m === "POST") return json(res, 200, linearPlan.check(pid, await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/integrations/atlassian[/authorize|/callback]` (docs/jira-sync/oauth.md#routes): the caller's Atlassian connection. */
  async function atlassianRoute({ req, res, action }) {
    const m = req.method;
    if (!action && m === "GET") return json(res, 200, jira.connection());
    if (!action && m === "DELETE") {
      jira.disconnect();
      res.writeHead(204);
      return res.end();
    }
    if (action === "authorize" && m === "POST") return json(res, 200, jira.authorize(await readJson(req)));
    if (action === "callback" && m === "POST") return json(res, 200, jira.callback(await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/projects/{pid}/integrations[/{app}]` (integrations-api.md): list, connect, remove. */
  async function integrationsRoute({ req, res, pid, did }) {
    const m = req.method;
    if (!did && m === "GET") return json(res, 200, integrations.list(pid));
    if (did && m === "PUT") return json(res, 200, integrations.connect(pid, did));
    if (did && m === "DELETE") {
      integrations.disconnect(pid, did);
      return noContent(res);
    }
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/projects/{pid}/slack[/test]` (docs/slack/gap-answering.md "The channel flow", integrations-api.md "Slack test message"). */
  async function slackRoute({ req, res, pid, did }) {
    const m = req.method;
    await drainBody(req);
    if (!did && m === "GET") return json(res, 200, integrations.slackGet(pid));
    if (!did && m === "DELETE") {
      integrations.slackUnbind(pid);
      return noContent(res);
    }
    if (did === "test" && m === "POST") return json(res, 200, integrations.slackTest(pid));
    return err(res, 404, "not_found");
  }

  const noContent = (res) => {
    res.writeHead(204);
    return res.end();
  };

  /** `/integrations/github[/authorize|/install|/callback|/setup|/repositories]` (contract.md#api): the caller's GitHub connection. */
  async function githubRoute({ req, res, action }) {
    const m = req.method;
    if (!action && m === "GET") return json(res, 200, github.connection());
    if (!action && m === "DELETE") {
      github.disconnect();
      return noContent(res);
    }
    if (action === "repositories" && m === "GET") return json(res, 200, github.available());
    if (action === "authorize" && m === "POST") return json(res, 200, github.authorize(await readJson(req)));
    if (action === "install" && m === "POST") return json(res, 200, github.install(await readJson(req)));
    if (action === "callback" && m === "POST") return json(res, 200, github.callback(await readJson(req)));
    if (action === "setup" && m === "POST") return json(res, 200, github.setup(await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/integrations/linear[/authorize|/callback]` (plans/LINEAR stream S1): the caller's Linear connection. */
  async function linearRoute({ req, res, action }) {
    const m = req.method;
    if (!action && m === "GET") return json(res, 200, linear.connection());
    if (!action && m === "DELETE") {
      linear.disconnect();
      return noContent(res);
    }
    if (action === "authorize" && m === "POST") return json(res, 200, linear.authorize(await readJson(req)));
    if (action === "callback" && m === "POST") return json(res, 200, linear.callback(await readJson(req)));
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  /** `/projects/{pid}/repositories[/{rid}[/clone]]` (contract.md#api). */
  async function repositoriesRoute({ req, res, pid, did, action }) {
    const m = req.method;
    if (!did && m === "GET") return json(res, 200, github.list(pid));
    if (!did && m === "POST") return json(res, 201, github.add(pid, await readJson(req)));
    if (did && !action && m === "DELETE") {
      github.remove(pid, did);
      return noContent(res);
    }
    if (did && action === "clone" && m === "POST") {
      await drainBody(req);
      return json(res, 202, github.clone(pid, did));
    }
    await drainBody(req);
    return err(res, 404, "not_found");
  }

  async function authOk(req) {
    const header = req.headers.authorization ?? "";
    if (!header.startsWith("Bearer ")) return false;
    if (!verify) return true;
    try {
      const { payload } = await jwtVerify(header.slice(7), keys, {
        issuer: verify.issuer,
        audience: verify.audience,
        algorithms: ["RS256"],
        clockTolerance: LEEWAY_SECONDS,
      });
      const clientIds = verify.clientIds ?? [DEFAULT_CLIENT_ID];
      return (
        typeof payload.azp === "string" &&
        clientIds.includes(payload.azp) &&
        payload.email_verified === true &&
        typeof payload.iat === "number" &&
        payload.iat <= Date.now() / 1000 + LEEWAY_SECONDS
      );
    } catch {
      return false;
    }
  }

  async function readBody(req) {
    const hash = crypto.createHash("sha256");
    let bytes = 0;
    let head = Buffer.alloc(0);
    for await (const chunk of req) {
      bytes += chunk.length;
      hash.update(chunk);
      if (head.length < 5) head = Buffer.concat([head, chunk]).subarray(0, 5);
    }
    return { bytes, sha256: hash.digest("hex"), head: head.toString("latin1") };
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://stub");
    const parts = url.pathname.split("/").filter(Boolean); // api v1 ...
    const entry = { method: req.method, path: url.pathname + url.search, headers: { ...req.headers } };
    log.push(entry);
    entry.authOk = await authOk(req);
    if (parts[0] !== "api" || parts[1] !== "v1") return err(res, 404, "not_found");
    if (!entry.authOk) {
      res.setHeader("www-authenticate", "Bearer");
      await readBody(req);
      return err(res, 401, "unauthenticated");
    }
    const [, , a, pid, b, did, action] = parts;

    if (a === "drive" && pid === "identity") {
      if (drive.identityReason) return err(res, REASON_HTTP[drive.identityReason], drive.identityReason);
      return json(res, 200, { email: drive.email });
    }
    if (a === "me") return json(res, 200, { id: "u1", actor_id: "human:dev", email: "dev@example.test", display_name: "dev" });

    return route({ req, res, url, a, pid, b, did, action, entry });
  });

  async function route(ctx) {
    try {
      return await routeInner(ctx);
    } catch (e) {
      if (!(e instanceof MembersError)) throw e;
      await drainBody(ctx.req);
      return err(ctx.res, e.status, e.code, e.details);
    }
  }

  async function routeInner({ req, res, url, a, pid, b, did, action, entry }) {
    if (a === "users") {
      if (pid !== "suggestions" || req.method !== "POST") {
        await drainBody(req);
        return err(res, 404, "not_found");
      }
      return json(res, 200, people.suggest(await readJson(req)));
    }
    if (a === "integrations" && pid === "atlassian") return atlassianRoute({ req, res, action: b });
    if (a === "integrations" && pid === "github") return githubRoute({ req, res, action: b });
    if (a === "integrations" && pid === "linear") return linearRoute({ req, res, action: b });
    if (a === "integrations" && pid === "notion") return notionRoute({ req, res, action: b });
    if (a !== "projects") return err(res, 404, "not_found");
    if (!pid) {
      if (req.method === "GET")
        return json(res, 200, { items: [...projects.values()].filter((p) => people.has(p.id)).reverse(), next_cursor: null });
      // POST /projects: {name, collaborators?} (docs/projects/api.md#create-with-collaborators)
      const body = await readJson(req);
      const extra = typeof body !== "object" || body === null || Object.keys(body).some((k) => k !== "name" && k !== "collaborators");
      const name = String(body?.name ?? "").trim();
      if (extra || !name || Array.from(name).length > 200) return err(res, 422, "validation_failed");
      const collaborators = people.collaboratorsOf(body);
      const project = { id: crypto.randomUUID(), name, created_at: new Date().toISOString() };
      projects.set(project.id, project);
      people.create(project.id, collaborators);
      return json(res, 201, project);
    }
    const project = projects.get(pid);
    if (!project || !people.has(pid)) {
      for await (const c of req) void c;
      return err(res, 404, "project_not_found"); // not a member (or never there): one answer
    }
    if (!b && req.method === "PATCH") {
      project.name = people.rename(pid, await readJson(req));
      return json(res, 200, project);
    }
    if (!b) return json(res, 200, project);
    if (b === "members" || b === "invites" || b === "leave") return membersRoute({ req, res, pid, b, did });
    if (b === "jira") return jiraRoute({ req, res, pid, did });
    if (b === "linear-gaps") return linearGapsRoute({ req, res, pid, did });
    if (b === "jira-gaps") return jiraGapsRoute({ req, res, pid, did });
    if (b === "github-projects") return githubProjectsRoute({ req, res, url, pid, did });
    if (b === "linear-plan") return linearPlanRoute({ req, res, url, pid, did });
    if (b === "notion-plan") return notionPlanRoute({ req, res, pid, did });
    if (b === "integrations") return integrationsRoute({ req, res, pid, did });
    // LINEAR S4: sources/linear (check, add) is served inside sourcesRoute, next to "jira-epic" (stub-linear-source.mjs).
    if (b === "slack") return slackRoute({ req, res, pid, did });
    if (b === "repositories") return repositoriesRoute({ req, res, pid, did, action });
    if (b === "sources") return sourcesRoute({ req, res, url, pid, did, action, entry });
    if (b === "graph_build") return graphRoute({ req, res, pid, entry });
    if (b === "findings" || (b === "plan" && did === "claims"))
      return workRoute({ req, res, url, pid, b, did, action, rest: url.pathname.split("/").filter(Boolean)[7] });
    if (b === "plan") return planRoute({ req, res, pid, did, entry });
    if (b === "gaps" || b === "gap") return gapRoute({ req, res, url, pid, b, did, action });
    if (b !== "documents") return err(res, 404, "not_found");

    if (!did) {
      if (req.method === "GET") {
        const items = [...docs.values()]
          .filter((d) => d.pid === pid)
          .reverse()
          .map(publicDoc);
        return json(res, 200, { items, next_cursor: null });
      }
      // POST upload (legacy, SPECS §4.5): raw body, application/pdf; fills the project's day batch.
      const up = await receiveFile({ req, url, entry });
      if (up.error) return err(res, ...up.error);
      const existing = [...docs.values()].find((d) => d.pid === pid && d.sha256 === up.body.sha256);
      // A re-upload of a failed document parses it again (backend `upload_document`); the rest stay plain duplicates.
      if (existing && existing.row.status !== "failed") return json(res, 200, { outcome: "duplicate", document: publicDoc(existing) });
      if (existing) {
        startParse(existing);
        return json(res, 200, { outcome: "retried", document: publicDoc(existing) });
      }
      const batch = model.legacyBatch(pid);
      const r = model.add(
        pid,
        batch.id,
        { filename: up.filename, sha256: up.body.sha256 },
        { makeDoc: (bid) => makeDoc(pid, bid, up), reparse: startParse, allowEnded: true },
      );
      return json(res, 201, { document: publicDoc(r.doc) });
    }

    const d = docs.get(did);
    if (!d || d.pid !== pid) {
      for await (const c of req) void c;
      return err(res, 404, "document_not_found");
    }
    for await (const c of req) void c;
    if (!action) return json(res, 200, d.row);
    if (action === "parse") {
      if (d.row.status === "parsed") return err(res, 409, "already_parsed");
      entry.parseCalls = true;
      if (d.row.status !== "processing") startParse(d);
      return json(res, 202, { workflow_id: `doc-parse-${did}`, status: "processing" });
    }
    if (action === "summarize") {
      if (d.row.status !== "parsed") return err(res, 409, "document_not_parsed");
      if (d.row.summarization_status !== "running") {
        d.row.summarization_status = "running";
        later(state.summarizeMs, () => {
          d.row.summarization_status = "succeeded";
          d.row.summarization = "A synthetic summary from the stub API.";
          d.row.summarization_model = "stub-model";
          d.row.summarized_at = new Date().toISOString();
        });
      }
      return json(res, 202, { workflow_id: `doc-summary-${did}`, summarization_status: "running" });
    }
    return err(res, 404, "not_found");
  }

  await listen(server, port);
  return {
    url: `http://127.0.0.1:${port}`,
    log,
    state,
    docs,
    sources,
    model,
    drive,
    /** Project collaborators: `people.state` (allowedDomains, directory, claimsReleased, claimsFailed) and `people.seed(pid, ...)`. */
    people,
    /** Jira settings: `jira.state` (available, mode, connection, callbackError) and `jira.seed({pid, available, target, access, mode, ...})`. */
    jira,
    /** Linear gap channel: `linearGaps.state` (available) and `linearGaps.seed({pid, available, target, access})`. */
    linearGaps,
    /** Jira gap channel: `jiraGaps.state` (available) and `jiraGaps.seed({pid, available, target, access})`. */
    jiraGaps,
    /** GitHub Projects destination: `githubProjects.state` (available, organization) and `githubProjects.seed({pid, available, organization, target, access})`. */
    githubProjects,
    /** Linear: `linear.state` (available, connection, callbackError), `linear.seed({available, connection, callbackError})`. */
    linear,
    /** Linear plan destination: `linearPlan.state` (available, workspace) and `linearPlan.seed({pid, available, workspace, target, access})`. */
    linearPlan,
    /** Notion: `notion.state` (enabled, connection, callbackError). */
    notion,
    /** Notion plan destination: `notionPlan.state` (available) and `notionPlan.seed({pid, available, target, access})`. */
    notionPlan,
    /** GitHub: `github.state` (enabled, connection, errors, available, cloneMs, cloneOutcome), `github.seed({pid, repositories, ...})`. */
    github,
    /** Project integrations: `integrations.state` (slackAvailable, slackTest) and `integrations.seed({pid, atlassian, github, slack, notion, ...})`. */
    integrations,
    /** Test seam: a project named `name` where the caller has `spec.role` (default owner), with the given members and invites. */
    seedProject: (name, spec) => {
      const project = { id: crypto.randomUUID(), name, created_at: new Date().toISOString() };
      projects.set(project.id, project);
      people.seed(project.id, spec);
      return project;
    },
    seedWork: (pid, work) => {
      const mine = workOf(pid);
      if (work.findings) mine.findings = work.findings;
      if (work.claims) mine.claims = work.claims;
    },
    seedGaps: (pid, seed) => {
      const mine = gapsOf(pid);
      if (seed.gaps) mine.gaps = seed.gaps;
      if (seed.run) {
        mine.run = seed.run;
        mine.stubRunMs = undefined; // a seeded run is the truth: the stub no longer moves it
      }
      if (seed.rules) mine.rules = seed.rules;
    },
    settleGaps,
    recordGaps: (pid, gaps) => {
      const mine = gapsOf(pid);
      const ids = new Set(gaps.map((g) => g.gap_id));
      mine.gaps = [...mine.gaps.filter((g) => !ids.has(g.gap_id)), ...gaps];
      if (mine.run.status === "running") mine.run = { ...mine.run, step_items_done: (mine.run.step_items_done ?? 0) + 1 };
    },
    setTiming: (t) => Object.assign(state, t),
    close: () => {
      github.close();
      return close(server);
    },
  };
}
