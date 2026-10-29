// Stub of the SPECS §9 HTTP API, close enough to drive the pages and the BFF.
// It is a test double only: the real API is built in S7. In-memory, one process.
import http from "node:http";
import crypto from "node:crypto";
import { jwtVerify } from "jose";
import { listen, close } from "./servers.mjs";
import { createSourcesModel, StubError } from "./stub-sources.mjs";

const json = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};
const err = (res, status, code) => json(res, status, { error: { code, message: code, details: {} } });

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
 * @param {{port:number, signingKey?:string, parseMs?:number, summarizeMs?:number}} opts
 * parseMs/summarizeMs = Infinity keeps the work "in progress" forever (stuck tests).
 */
export async function startStubApi({ port, signingKey, parseMs = 1500, summarizeMs = 1500, parseOutcome = "parsed" }) {
  const projects = new Map();
  const docs = new Map(); // id -> doc
  const log = []; // {method, path, headers, authOk, contentType, bytes}
  const state = { parseMs, summarizeMs, parseOutcome };
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
    if (req.headers["content-type"] !== "application/pdf" || (body.bytes && body.head !== "%PDF-"))
      return { error: [415, "unsupported_format"] };
    if (body.bytes === 0) return { error: [400, "empty_file"] };
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
    if (up.error) return err(res, up.error[0], up.error[1]);
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
    if (!signingKey) return true;
    try {
      await jwtVerify(header.slice(7), new TextEncoder().encode(signingKey), {
        issuer: "specflow-dev-login",
        audience: "specflow-api",
      });
      return true;
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
      if (up.error) return err(res, up.error[0], up.error[1]);
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
