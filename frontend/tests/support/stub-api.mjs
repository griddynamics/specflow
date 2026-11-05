// Stub of the SPECS §9 HTTP API, close enough to drive the pages and the BFF.
// It is a test double only: the real API is built in S7. In-memory, one process.
import http from "node:http";
import crypto from "node:crypto";
import { createLocalJWKSet, jwtVerify } from "jose";
import { listen, close } from "./servers.mjs";
import { createSourcesModel, StubError } from "./stub-sources.mjs";

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
 * Plan knob: planMs (a plan run takes that long, its ten steps evenly spread; see `planRoute`).
 */
export async function startStubApi({ port, verify, parseMs = 1500, summarizeMs = 1500, parseOutcome = "parsed" }) {
  const keys = verify ? createLocalJWKSet(verify.jwks) : undefined;
  const projects = new Map();
  const docs = new Map(); // id -> doc
  const log = []; // {method, path, headers, authOk, contentType, bytes}
  const state = { parseMs, summarizeMs, parseOutcome, graphMs: 1500, graphOutcome: "succeeded", graphStartFail: null, planMs: 2000 };
  /** Drive: sources per project (SourceOut rows, mutable by tests), and the identity answer (a Reason string = 503 with that code). */
  const sources = new Map(); // pid -> SourceOut[]
  const drive = { identityReason: null, email: "specflow-sa@example.test" };
  const sourcesOf = (pid) => sources.get(pid) ?? sources.set(pid, []).get(pid);
  /** sources-ux (SPECS §4): the v2 status, file list, removal and Local files batches. Documents of an upload carry `batchId`. */
  const model = createSourcesModel({ docs, sourcesOf });
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
      else g.succeededBefore = true;
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
      if (!did && req.method === "GET") {
        await drain();
        return json(res, 200, url.searchParams.get("batches") === "1" ? model.status(pid) : progressOf(pid));
      }
      if (did === "uploads" && req.method === "POST" && !action) {
        const b = model.start(pid, (await readJson())?.expected_files);
        return json(res, 201, { source: model.summaries(pid).find((s) => s.id === b.id) });
      }
      if (did && did !== "gdrive" && did !== "uploads") {
        if (!action && req.method === "DELETE") {
          await drain();
          model.remove(pid, did);
          res.writeHead(204);
          return res.end();
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
    const link = JSON.parse(Buffer.concat(chunks).toString() || "{}").link;
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
      });
    }
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

  async function route({ req, res, url, a, pid, b, did, action, entry }) {
    if (a !== "projects") return err(res, 404, "not_found");
    if (!pid) {
      if (req.method === "GET") return json(res, 200, { items: [...projects.values()].reverse(), next_cursor: null });
      // POST /projects
      const chunks = [];
      for await (const c of req) chunks.push(c);
      const name = String(JSON.parse(Buffer.concat(chunks).toString() || "{}").name ?? "").trim();
      if (!name || name.length > 200) return err(res, 422, "validation_failed");
      const project = { id: crypto.randomUUID(), name, created_at: new Date().toISOString() };
      projects.set(project.id, project);
      return json(res, 201, project);
    }
    const project = projects.get(pid);
    if (!project) {
      for await (const c of req) void c;
      return err(res, 404, "project_not_found");
    }
    if (!b) return json(res, 200, project);
    if (b === "sources") return sourcesRoute({ req, res, url, pid, did, action, entry });
    if (b === "graph_build") return graphRoute({ req, res, pid, entry });
    if (b === "plan") return planRoute({ req, res, pid, did, entry });
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
    setTiming: (t) => Object.assign(state, t),
    close: () => close(server),
  };
}
