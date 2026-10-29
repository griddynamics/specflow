// INT-SRC-0001..0004, DATA-SRC-0001 (sources-ux SPECS §3.3): parity of the production adapter and of the stub API with
// backend/src/api/fixtures/sources-contract.json (session 01). A missing contract file fails the test.
// The stub API is called directly (port 3254), no BFF.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { ApiError } from "@/lib/api";
import { toDataError } from "@/lib/data/http/errors";
import { toStatus } from "@/lib/data/http/knowledge";
import { BATCH_OUTCOMES, FILE_FIELDS, HOLDER_FIELDS, SOURCE_FIELDS, toFilePage, toSource } from "@/lib/data/http/sources-wire";
import { FILE_STATES, GROUP_ORDER, PAGE_LIMIT, REASONS, groupOf } from "@/lib/domain/source-state";
import { startStubApi } from "../support/stub-api.mjs";

const CONTRACT = path.resolve(__dirname, "../../../backend/src/api/fixtures/sources-contract.json");
interface Contract {
  file_states: string[];
  reason_codes: string[];
  group_order: string[][];
  page_limit: number;
  outcomes: string[];
  error_codes: string[];
  source_fields: string[];
  file_fields: string[];
  holder_fields: string[];
}
const contract = JSON.parse(readFileSync(CONTRACT, "utf8")) as Contract;

const PORT = 3254;
const BASE = `http://127.0.0.1:${String(PORT)}/api/v1`;
const auth = { authorization: "Bearer stub", "content-type": "application/json" };
let api: Awaited<ReturnType<typeof startStubApi>> | undefined;
before(async () => {
  api = await startStubApi({ port: PORT, parseMs: 50 });
});
after(async () => {
  await api?.close();
});
// The shape of the stub's JSON answers that these tests read; a body-less answer (204) parses to EMPTY.
interface Wire {
  id: string;
  outcome: string;
  holder: { id: string };
  file: { holder: { id: string } };
  source: { id: string; counts: Record<string, number> };
  sources: { counts: Record<string, number> }[];
  items: { holder: { id: string } }[];
  removed_files: number;
  error?: { code: string };
}
const EMPTY: Wire = {
  id: "",
  outcome: "",
  holder: { id: "" },
  file: { holder: { id: "" } },
  source: { id: "", counts: {} },
  sources: [],
  items: [],
  removed_files: 0,
};
const call = async (method: string, p: string, body?: BodyInit, headers: Record<string, string> = auth) => {
  const res = await fetch(`${BASE}${p}`, { method, headers, body });
  const text = await res.text();
  const parsed: Wire = text ? (JSON.parse(text) as Wire) : EMPTY;
  return { status: res.status, body: parsed };
};
const sorted = (a: readonly string[]) => [...a].toSorted((a, b) => a.localeCompare(b));

test("parity: file states, reasons, group order and page limit equal the contract", () => {
  assert.deepEqual([...FILE_STATES], contract.file_states);
  assert.deepEqual(sorted(REASONS), sorted(contract.reason_codes));
  assert.equal(PAGE_LIMIT, contract.page_limit);
  // The contract lists the states of each group in order; the TS rules (`groupOf`) must put the same states in the same group order.
  assert.equal(GROUP_ORDER.length, contract.group_order.length);
  GROUP_ORDER.forEach((group, i) => {
    assert.deepEqual(sorted(FILE_STATES.filter((s) => groupOf(s) === group)), sorted(contract.group_order[i]), `group ${group}`);
  });
  assert.deepEqual(contract.group_order[0], ["not_read", "skipped", "failed", "already_in_project"], "problem files first, in this order");
});

test("parity: the outcomes and the field lists the adapter parses equal the contract", () => {
  assert.deepEqual([...BATCH_OUTCOMES], contract.outcomes);
  assert.deepEqual([...SOURCE_FIELDS], contract.source_fields);
  assert.deepEqual([...FILE_FIELDS], contract.file_fields);
  assert.deepEqual([...HOLDER_FIELDS], contract.holder_fields);
});

test("parity: every contract error code has a DataError meaning and keeps its apiCode; source_not_found is not not_found", () => {
  for (const code of contract.error_codes) {
    const e = toDataError(new ApiError(code === "source_not_found" ? 404 : 400, code));
    assert.equal(e.detail?.apiCode, code, code);
    assert.notEqual(e.code, "unknown", code);
  }
  assert.equal(toDataError(new ApiError(404, "source_not_found")).code, toDataError(new ApiError(404, "not_found")).code);
  assert.notEqual(
    toDataError(new ApiError(404, "source_not_found")).detail?.apiCode,
    toDataError(new ApiError(404, "not_found")).detail?.apiCode,
  );
});

test("the stub API serves every §4 endpoint with exactly the contract's fields, outcomes and error codes", async () => {
  const pid = (await call("POST", "/projects", JSON.stringify({ name: "Contract" }))).body.id;
  const pdf = (n: number) => Buffer.concat([Buffer.from("%PDF-1.7\n"), Buffer.alloc(200 + n, 0x41)]);
  const pdfHeaders = { ...auth, "content-type": "application/pdf" };
  const codes = new Set<string>();
  const failure = (r: { status: number; body: Wire }) => (r.body.error ? codes.add(r.body.error.code) : undefined);

  // 4.4 start, add (added / duplicate / already_in_project), end.
  const started = await call("POST", `/projects/${pid}/sources/uploads`, JSON.stringify({ expected_files: 4 }));
  assert.equal(started.status, 201);
  assert.deepEqual(sorted(Object.keys(started.body.source)), sorted(contract.source_fields));
  const sid = started.body.source.id;
  const added = await call("POST", `/projects/${pid}/sources/${sid}/files?filename=a.pdf`, pdf(1), pdfHeaders);
  assert.deepEqual([added.status, added.body.outcome, added.body.holder], [201, "added", null]);
  assert.deepEqual(sorted(Object.keys(added.body.file)), sorted(contract.file_fields));
  const dup = await call("POST", `/projects/${pid}/sources/${sid}/files?filename=a2.pdf`, pdf(1), pdfHeaders);
  assert.deepEqual([dup.status, dup.body], [200, { outcome: "duplicate", file: null, holder: null }]);
  const second = (await call("POST", `/projects/${pid}/sources/uploads`, JSON.stringify({ expected_files: 1 }))).body.source.id;
  const held = await call("POST", `/projects/${pid}/sources/${second}/files?filename=a3.pdf`, pdf(1), pdfHeaders);
  assert.deepEqual([held.status, held.body.outcome], [200, "already_in_project"]);
  assert.deepEqual(sorted(Object.keys(held.body.holder)), sorted(contract.holder_fields));
  assert.equal(held.body.file.holder.id, sid);
  assert.deepEqual(new Set(["added", "duplicate", "already_in_project"]).difference(new Set(contract.outcomes)).size, 0);
  assert.equal((await call("POST", `/projects/${pid}/sources/${sid}/end`)).status, 200);
  failure(await call("POST", `/projects/${pid}/sources/${sid}/files?filename=late.pdf`, pdf(2), pdfHeaders));
  failure(await call("POST", `/projects/${pid}/sources/00000000-0000-4000-8000-000000000000/files?filename=x.pdf`, pdf(3), pdfHeaders));
  failure(await call("POST", `/projects/${pid}/sources/${second}/files`, pdf(4), pdfHeaders));
  failure(await call("POST", `/projects/${pid}/sources/${second}/files?filename=x.pdf`, Buffer.alloc(0), pdfHeaders));
  failure(
    await call("POST", `/projects/${pid}/sources/${second}/files?filename=x.txt`, Buffer.from("plain text, not a PDF"), {
      ...auth,
      "content-type": "text/plain",
    }),
  );
  failure(await call("POST", `/projects/${pid}/sources/uploads`, JSON.stringify({ expected_files: 0 })));
  assert.equal((await call("POST", `/projects/${pid}/sources/${second}/end`)).status, 200);
  // 4.4: ending a batch with no accepted file deletes it (204).
  const empty = (await call("POST", `/projects/${pid}/sources/uploads`, JSON.stringify({ expected_files: 1 }))).body.source.id;
  assert.equal((await call("POST", `/projects/${pid}/sources/${empty}/end`)).status, 204);

  // 4.1 the status.
  const status = await call("GET", `/projects/${pid}/sources?batches=1`);
  assert.deepEqual(sorted(Object.keys(status.body)), sorted(["progress", "processed_documents", "removed_files", "sources"]));
  assert.equal(status.body.sources.length, 2, "the deleted empty batch is hidden");
  for (const s of status.body.sources) assert.deepEqual(sorted(Object.keys(s)), sorted(contract.source_fields));
  assert.deepEqual(
    Object.keys(status.body.sources[0].counts).toSorted((a, b) => a.localeCompare(b)),
    sorted(contract.file_states),
  );
  const parsed = toStatus(status.body);
  assert.equal(parsed.sources.length, 2);
  assert.ok(
    parsed.sources.every((s) => Object.values(s.counts).reduce((a, b) => a + b, 0) === s.total),
    "sum(counts) == total",
  );
  // The same url without the flag is the legacy body (SPECS §4.5): it must NOT parse as v2.
  const legacy = await call("GET", `/projects/${pid}/sources`);
  assert.throws(
    () => toStatus(legacy.body),
    (e: unknown) => (e as { code?: string }).code === "invalid_response",
  );

  // 4.2 the file list.
  const files = await call("GET", `/projects/${pid}/sources/${second}/files?limit=200`);
  assert.deepEqual(sorted(Object.keys(files.body)), sorted(["source", "items", "next_cursor"]));
  assert.deepEqual(sorted(Object.keys(files.body.items[0])), sorted(contract.file_fields));
  assert.deepEqual(sorted(Object.keys(files.body.items[0].holder)), sorted(contract.holder_fields));
  assert.equal(toFilePage(files.body).items.length, 1);
  assert.equal(toSource(files.body.source).id, second);
  failure(await call("GET", `/projects/${pid}/sources/${second}/files?cursor=%25%25`));
  failure(await call("GET", `/projects/${pid}/sources/00000000-0000-4000-8000-000000000000/files`));

  // 4.3 removal.
  assert.equal((await call("DELETE", `/projects/${pid}/sources/${second}`)).status, 204);
  failure(await call("DELETE", `/projects/${pid}/sources/${second}`));
  failure(await call("GET", `/projects/00000000-0000-4000-8000-000000000000/sources?batches=1`));
  assert.equal((await call("GET", `/projects/${pid}/sources?batches=1`)).body.removed_files, 1);

  for (const code of codes) assert.ok(contract.error_codes.includes(code), `the stub answers ${code}, which is not in the contract`);
  assert.deepEqual(
    sorted([...codes]),
    sorted([
      "batch_ended",
      "source_not_found",
      "missing_filename",
      "empty_file",
      "unsupported_format",
      "validation_failed",
      "project_not_found",
    ]),
    "every code the stub can answer here was exercised",
  );
});
