// Stub of the sources-ux API (SPECS §4.1-§4.4), in memory: the status read with `?batches=1`, the paged file list, DELETE source and the
// Local files batch (start, add, end). A test double next to stub-api.mjs, close enough to drive the http adapter and the BFF; the real
// API is built in session 04. Key lists and codes follow backend/src/api/fixtures/sources-contract.json (a test compares them).
import crypto from "node:crypto";

const USER = { id: "11111111-1111-4111-8111-111111111111", display_name: "dev" };
const RANK = { not_read: 0, skipped: 0, failed: 0, already_in_project: 0, processing: 1, waiting: 2, done: 3 };
const STATES = ["waiting", "processing", "done", "not_read", "skipped", "failed", "already_in_project"];
const PARSED = new Set(["parsed", "ingested", "partially_ingested"]);
const MAX_PAGE = 200;

/** Thrown for an error answer: the route turns it into the API envelope. */
export class StubError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

/**
 * @param {{docs: Map<string, any>, sourcesOf: (pid: string) => any[]}} deps docs = the stub's documents, sourcesOf = its Drive rows (legacy SourceOut).
 */
export function createSourcesModel({ docs, sourcesOf }) {
  const batches = new Map(); // pid -> batch[]
  const removed = new Map(); // pid -> files of removed sources
  const seqs = new WeakMap(); // row or batch -> creation order (Drive rows and batches share one added order)
  let seq = 0;
  const stamp = (o) => (seqs.set(o, ++seq), o);
  const batchesOf = (pid) => batches.get(pid) ?? batches.set(pid, []).get(pid);

  const driveSeq = (row) => (seqs.has(row) ? seqs.get(row) : seqs.get(stamp(row)));

  // ---- files ----
  const docState = (d) => {
    const s = d.row.status;
    if (PARSED.has(s)) return { state: "done", reason: null };
    if (s === "failed") return { state: "failed", reason: d.row.error === "pdf_parse_failed" ? "read_failed" : "unknown" };
    return { state: "processing", reason: null };
  };
  const holderRef = (pid, id) => {
    const b = batchesOf(pid).find((x) => x.id === id);
    if (b) return { id: b.id, kind: "upload", name: "Local files", name_suffix: b.suffix, created_at: b.created_at };
    const row = sourcesOf(pid).find((x) => x.id === id);
    return row ? { id: row.id, kind: "gdrive", name: row.name, name_suffix: 1, created_at: row.created_at } : null;
  };
  const fileOfItem = (pid, item) => {
    if (item.fixed)
      return {
        id: item.id,
        name: item.name,
        category: item.fixed.category ?? "pdf",
        state: item.fixed.state,
        reason: item.fixed.reason ?? null,
        holder: item.fixed.holder ? holderRef(pid, item.fixed.holder) : null,
      };
    if (item.holder)
      return {
        id: item.id,
        name: item.name,
        category: "pdf",
        state: "already_in_project",
        reason: "already_in_project",
        holder: holderRef(pid, item.holder),
      };
    const d = docs.get(item.docId);
    return { id: item.id, name: item.name, category: "pdf", ...(d ? docState(d) : { state: "failed", reason: "unknown" }), holder: null };
  };
  const filesOf = (pid, sid) => {
    const b = batchesOf(pid).find((x) => x.id === sid);
    if (b) return b.items.map((i) => fileOfItem(pid, i));
    const row = sourcesOf(pid).find((x) => x.id === sid);
    return row?._files ?? null;
  };

  // ---- summaries ----
  const countsOf = (files) => {
    const counts = Object.fromEntries(STATES.map((s) => [s, 0]));
    for (const f of files) counts[f.state]++;
    return counts;
  };
  const isOpen = (b) => !b.ended;
  const batchSummary = (pid, b) => {
    const counts = countsOf(b.items.map((i) => fileOfItem(pid, i)));
    if (isOpen(b)) counts.waiting += Math.max(b.expected - b.items.length, 0);
    const total = STATES.reduce((n, s) => n + counts[s], 0);
    return {
      id: b.id,
      kind: "upload",
      name: "Local files",
      name_suffix: b.suffix,
      added_by: USER,
      created_at: b.created_at,
      listing_finished: true,
      open: isOpen(b),
      total,
      counts,
      failure_reason: null,
      folder_id: null,
      link: null,
    };
  };
  const driveSummary = (row) => {
    const p = row.progress;
    let counts;
    if (row._files) counts = countsOf(row._files);
    else {
      counts = Object.fromEntries(STATES.map((s) => [s, 0]));
      counts.done = p.finished;
      counts[row.state === "failed" ? "failed" : "waiting"] = p.total - p.finished;
    }
    const total = STATES.reduce((n, s) => n + counts[s], 0);
    return {
      id: row.id,
      kind: "gdrive",
      name: row.name,
      name_suffix: 1,
      added_by: { ...USER, id: row.connected_by },
      created_at: row.created_at,
      listing_finished: p.listing_finished,
      open: false,
      total,
      counts,
      failure_reason: row.failure_reason ?? null,
      folder_id: row.folder_id,
      link: row.link,
    };
  };
  const hidden = (b) => b.ended && b.items.length === 0;
  /** Every source of the project in added order (hidden empty batches left out). */
  const summaries = (pid) =>
    [
      ...batchesOf(pid)
        .filter((b) => !hidden(b))
        .map((b) => ({ at: seqs.get(b), s: batchSummary(pid, b) })),
      ...sourcesOf(pid).map((r) => ({ at: driveSeq(r), s: driveSummary(r) })),
    ]
      .sort((a, b) => a.at - b.at)
      .map((x) => x.s);

  function status(pid) {
    const sources = summaries(pid);
    const total = sources.reduce((n, s) => n + s.total, 0);
    const open = sources.reduce((n, s) => n + s.counts.waiting + s.counts.processing, 0);
    return {
      progress: { listing_finished: sources.every((s) => s.listing_finished), total, finished: total - open },
      processed_documents: [...docs.values()].filter((d) => d.pid === pid && PARSED.has(d.row.status)).length,
      removed_files: removed.get(pid) ?? 0,
      sources,
    };
  }

  const keyOf = (f) => [RANK[f.state], f.name.toLowerCase(), f.id];
  const cmp = (a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0);
  function page(pid, sid, limit, cursor) {
    const summary = summaries(pid).find((s) => s.id === sid);
    if (!summary) throw new StubError(404, "source_not_found");
    if (!Number.isInteger(limit) || limit < 1 || limit > MAX_PAGE) throw new StubError(422, "validation_failed");
    let after = null;
    if (cursor) {
      try {
        after = JSON.parse(Buffer.from(cursor, "base64url").toString());
        if (!Array.isArray(after) || after.length !== 3) throw new Error("shape");
      } catch {
        throw new StubError(422, "validation_failed");
      }
    }
    const sorted = (filesOf(pid, sid) ?? []).sort((a, b) => cmp(keyOf(a), keyOf(b)));
    const rest = after ? sorted.filter((f) => cmp(keyOf(f), after) > 0) : sorted;
    const items = rest.slice(0, limit);
    const last = items.at(-1);
    return {
      source: summary,
      items,
      next_cursor: rest.length > limit && last ? Buffer.from(JSON.stringify(keyOf(last))).toString("base64url") : null,
    };
  }

  // ---- batches ----
  function dropHidden(pid) {
    const list = batchesOf(pid);
    batches.set(
      pid,
      list.filter((b) => !hidden(b)),
    );
  }
  function start(pid, expected) {
    if (!Number.isInteger(expected) || expected < 1 || expected > 10000) throw new StubError(422, "validation_failed");
    dropHidden(pid);
    const minute = new Date().toISOString().slice(0, 16);
    const same = batchesOf(pid).filter((b) => b.created_at.slice(0, 16) === minute);
    const b = stamp({
      id: crypto.randomUUID(),
      created_at: new Date().toISOString(),
      suffix: 1 + Math.max(0, ...same.map((x) => x.suffix)),
      expected,
      ended: false,
      items: [],
    });
    batchesOf(pid).push(b);
    return b;
  }
  /** The batch the legacy `POST /documents` fills: one closed day batch per project (SPECS §4.5). */
  function legacyBatch(pid) {
    const found = batchesOf(pid).find((b) => b.legacy);
    if (found) return found;
    const b = stamp({
      id: crypto.randomUUID(),
      created_at: new Date().toISOString(),
      suffix: 1,
      expected: 1,
      ended: true,
      legacy: true,
      items: [],
    });
    batchesOf(pid).push(b);
    return b;
  }
  function end(pid, sid) {
    const b = batchesOf(pid).find((x) => x.id === sid);
    if (!b) throw new StubError(404, "source_not_found");
    b.ended = true;
    if (b.items.length === 0) {
      batches.set(
        pid,
        batchesOf(pid).filter((x) => x !== b),
      );
      return null;
    }
    return batchSummary(pid, b);
  }
  /**
   * One accepted file (§4.4 add rules, simplified): `fresh()` makes the document. Returns {outcome, file, holder, doc, reparse}.
   * `reparse(doc)` starts the parse again for a moved failed document.
   */
  function add(pid, sid, { filename, sha256 }, { makeDoc, reparse, allowEnded = false }) {
    const b = batchesOf(pid).find((x) => x.id === sid);
    if (!b) throw sourcesOf(pid).some((r) => r.id === sid) ? new StubError(409, "batch_ended") : new StubError(404, "source_not_found");
    if (b.ended && !allowEnded) throw new StubError(409, "batch_ended");
    if (b.items.some((i) => i.sha256 === sha256))
      return { outcome: "duplicate", file: null, holder: null, doc: [...docs.values()].find((d) => d.pid === pid && d.sha256 === sha256) };
    const existing = [...docs.values()].find((d) => d.pid === pid && d.sha256 === sha256);
    if (existing && existing.row.status === "failed") {
      const old = batchesOf(pid).find((x) => x.id === existing.batchId);
      if (old) old.items = old.items.filter((i) => i.docId !== existing.row.id);
      existing.batchId = b.id;
      const item = { id: existing.row.id, name: filename, docId: existing.row.id, sha256, holder: null, fixed: null };
      b.items.push(item);
      if (old && old !== b && old.ended && old.items.length === 0)
        batches.set(
          pid,
          batchesOf(pid).filter((x) => x !== old),
        );
      reparse(existing);
      return { outcome: "read_again", file: fileOfItem(pid, item), holder: null, doc: existing };
    }
    if (existing) {
      const item = { id: crypto.randomUUID(), name: filename, docId: existing.row.id, sha256, holder: existing.batchId, fixed: null };
      b.items.push(item);
      return { outcome: "already_in_project", file: fileOfItem(pid, item), holder: holderRef(pid, existing.batchId), doc: existing };
    }
    const doc = makeDoc(b.id);
    const item = { id: doc.row.id, name: filename, docId: doc.row.id, sha256, holder: null, fixed: null };
    b.items.push(item);
    return { outcome: "added", file: fileOfItem(pid, item), holder: null, doc };
  }

  // ---- removal and seeding (the control endpoint of the tests) ----
  function remove(pid, sid) {
    const b = batchesOf(pid).find((x) => x.id === sid);
    let n = 0;
    if (b) {
      n = b.items.length;
      for (const i of b.items) if (i.docId && docs.get(i.docId)?.batchId === b.id) docs.delete(i.docId);
      batches.set(
        pid,
        batchesOf(pid).filter((x) => x !== b),
      );
    } else {
      const rows = sourcesOf(pid);
      const at = rows.findIndex((r) => r.id === sid);
      if (at < 0) throw new StubError(404, "source_not_found");
      n = rows[at]._files?.length ?? rows[at].progress.total;
      rows.splice(at, 1);
    }
    removed.set(pid, (removed.get(pid) ?? 0) + n);
  }

  /** spec: {kind:"gdrive"|"upload", name?, listing?, failure_reason?, open?, expected?, files:[{name,state,reason?,category?,holder?}]}. Returns the id. */
  function seed(pid, spec) {
    const files = (spec.files ?? []).map((f) => ({
      id: crypto.randomUUID(),
      name: f.name,
      category: f.category ?? "pdf",
      state: f.state,
      reason: f.reason ?? null,
      holder: f.holder ?? null,
    }));
    if (spec.kind === "upload") {
      const b = stamp({
        id: crypto.randomUUID(),
        created_at: new Date().toISOString(),
        suffix: spec.name_suffix ?? 1,
        expected: spec.expected ?? files.length,
        ended: !spec.open,
        items: [],
      });
      b.items = files.map((f) => ({
        id: f.id,
        name: f.name,
        docId: null,
        sha256: f.id,
        holder: null,
        fixed: { state: f.state, reason: f.reason, category: f.category, holder: f.holder },
      }));
      batchesOf(pid).push(b);
      return b.id;
    }
    const finished = files.filter((f) => f.state !== "waiting" && f.state !== "processing").length;
    const row = stamp({
      id: crypto.randomUUID(),
      kind: "gdrive",
      folder_id: `seed-${crypto.randomUUID().slice(0, 8)}`,
      link: "https://drive.google.com/drive/folders/seeded-folder",
      name: spec.name ?? "Seeded folder",
      state: spec.failure_reason ? "failed" : spec.listing ? "listing" : finished < files.length ? "importing" : "done",
      failure_reason: spec.failure_reason ?? null,
      connected_by: USER.id,
      created_at: new Date().toISOString(),
      last_synced_at: null,
      progress: { listing_finished: !spec.listing, total: files.length, finished },
    });
    Object.defineProperty(row, "_files", { value: files, enumerable: false });
    sourcesOf(pid).push(row);
    return row.id;
  }

  return { status, page, start, end, add, remove, seed, legacyBatch, summaries, stamp };
}
