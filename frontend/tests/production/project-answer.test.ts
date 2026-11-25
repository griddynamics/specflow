// The V2 project gate (app/(v2)/projects/[projectId]/layout.tsx): which answers of `GET /projects/{id}` mean "not yours" (404) or
// "not an id" (422, a malformed id on the real API), and that any other failure still lets the page render.
import assert from "node:assert/strict";
import { test } from "node:test";
import { projectNameOf } from "@/lib/project-answer";

test("404 and 422 are 'not_found': an unknown id and an id that is not a UUID", () => {
  assert.equal(projectNameOf({ status: 404, body: "" }), "not_found");
  assert.equal(projectNameOf({ status: 422, body: '{"detail":[{"type":"uuid_parsing"}]}' }), "not_found");
});

test("200 gives the name; a body without one, or not JSON, is unreadable", () => {
  assert.deepEqual(projectNameOf({ status: 200, body: '{"name":"Northwind"}' }), { name: "Northwind" });
  assert.equal(projectNameOf({ status: 200, body: "{}" }), null);
  assert.equal(projectNameOf({ status: 200, body: "<html>" }), null);
});

test("any other status leaves the page to render and show its own failure", () => {
  for (const status of [401, 403, 500, 502, 503]) assert.equal(projectNameOf({ status, body: "" }), null, String(status));
});
