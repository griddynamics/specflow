// FE-DATA-01, FE-BP-02, FE-BP-08 (TECH-SPECS §2.13): toDataError maps ApiError (code first, then status), fetch failures and
// aborts to DataError codes; the retryable flag follows the §2.13 table. Pure: no server.
import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "@/lib/api";
import { checked, guard, toDataError } from "@/lib/data/http/errors";
import { toPerson } from "@/lib/data/http/me";
import { backendName, toProject } from "@/lib/data/http/projects";
import { DataError, isDataError } from "@/lib/domain/errors";

const cases: [number, string, string, boolean][] = [
  [401, "unauthenticated", "unauthenticated", false],
  [404, "project_not_found", "not_found", false],
  [404, "document_not_found", "not_found", false],
  [404, "not_found", "not_found", false],
  [409, "already_parsed", "conflict", false],
  [409, "document_not_parsed", "conflict", false],
  [422, "validation_failed", "validation", false],
  [422, "model_not_allowed", "validation", false],
  [400, "missing_filename", "validation", false],
  [400, "invalid_path", "validation", false],
  [415, "unsupported_format", "unsupported_format", false],
  [400, "empty_file", "empty_file", false],
  [502, "upstream_unavailable", "unavailable", true],
  [503, "workflow_start_failed", "unavailable", true],
  [503, "database_unavailable", "unavailable", true],
  [200, "invalid_response", "invalid_response", true],
  [403, "csrf_rejected", "unknown", false],
];

for (const [status, code, want, retryable] of cases) {
  test(`FE-DATA-01 toDataError: ${String(status)} ${code} -> ${want} (retryable ${String(retryable)})`, () => {
    const e = toDataError(new ApiError(status, code));
    assert.ok(e instanceof DataError);
    assert.equal(e.code, want);
    assert.equal(e.retryable, retryable);
    assert.deepEqual(e.detail, { status, apiCode: code });
    assert.equal(e.message, want, "a code, never server text");
  });
}

test("FE-DATA-01 toDataError: unknown codes fall back to the HTTP status", () => {
  const by = (status: number) => toDataError(new ApiError(status, `http_${String(status)}`)).code;
  assert.equal(by(401), "unauthenticated");
  assert.equal(by(404), "not_found");
  assert.equal(by(409), "conflict");
  assert.equal(by(415), "unsupported_format");
  assert.equal(by(400), "validation");
  assert.equal(by(422), "validation");
  assert.equal(by(500), "unavailable");
  assert.equal(by(504), "unavailable");
  assert.equal(toDataError(new ApiError(504, "http_504")).retryable, true);
  assert.equal(by(403), "unknown");
  assert.equal(by(418), "unknown");
});

test("FE-DATA-01 guard: a TypeError from the lib/api.ts call (its fetch rejected) -> network; toDataError alone -> unknown (QA m1)", async () => {
  await assert.rejects(
    guard(() => Promise.reject(new TypeError("fetch failed"))),
    (e) => isDataError(e) && e.code === "network" && e.retryable,
  );
  assert.equal(toDataError(new TypeError("Cannot read properties of null")).code, "unknown");
  assert.throws(
    () => checked(() => (JSON.parse("null") as { a: number }).a),
    (e) => isDataError(e) && e.code === "invalid_response" && e.retryable,
  );
});

test("FE-DATA-01 toDataError: abort -> aborted, DataError passes through, other -> unknown", () => {
  const abort = new Error("aborted");
  abort.name = "AbortError";
  assert.equal(toDataError(abort).code, "aborted");
  assert.equal(toDataError(new DOMException("x", "AbortError")).code, "aborted");
  const same = new DataError("conflict", false);
  assert.equal(toDataError(same), same);
  assert.equal(toDataError("boom").code, "unknown");
  assert.equal(toDataError(new Error("server text")).message, "unknown");
});

test("FE-PPL-01.AC3 toPerson / FE-BP-02 toProject: API shapes -> domain (status unknown, no content set)", () => {
  assert.deepEqual(toPerson({ id: "u1", actor_id: "human:dev", email: "dev@example.test", display_name: "dev" }), {
    id: "u1",
    name: "dev",
    email: "dev@example.test",
    kind: "person",
  });
  assert.equal(toPerson({ id: "u2", email: "a@b.test", display_name: " " }).name, "a@b.test");
  for (const bad of [{}, null, [], 5, { id: "" }]) {
    assert.throws(
      () => toPerson(bad),
      (e) => isDataError(e) && e.code === "invalid_response",
      `me ${JSON.stringify(bad)}`,
    );
    assert.throws(
      () => toProject(bad),
      (e) => isDataError(e) && e.code === "invalid_response",
      `project ${JSON.stringify(bad)}`,
    );
  }
  assert.deepEqual(toProject({ id: "p", name: "Pilot", created_at: "2026-10-09T00:00:00Z" }), {
    id: "p",
    name: "Pilot",
    createdAt: "2026-10-09T00:00:00Z",
    status: "unknown",
  });
});

test("FE-BP-02.AC4: the name is counted in code points like the backend: 150 emoji pass, 201 code points fail, blank fails", () => {
  assert.equal(backendName("\u{1F600}".repeat(150)), "\u{1F600}".repeat(150));
  assert.equal(backendName("\u{1F600}".repeat(200)), "\u{1F600}".repeat(200));
  assert.equal(backendName("\u{1F600}".repeat(201)), null);
  assert.equal(backendName("x".repeat(201)), null);
  assert.equal(backendName("  Pilot "), "Pilot");
  assert.equal(backendName("   "), null);
});
