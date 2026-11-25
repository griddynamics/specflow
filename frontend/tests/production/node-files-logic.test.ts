// What the node dialog says of the files inside a source and of a refused rename: the rows are the V1 words over the Graphico list.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileRowOf, kindOf, renameProblem } from "@/components/v2/canvas/node-files-logic";
import { CANVAS_COPY } from "@/components/v2/canvas/canvas-copy";
import { DataError } from "@/lib/domain/errors";
import type { SourceFile } from "@/lib/domain/sources";

const file = (over: Partial<SourceFile>): SourceFile => ({
  id: "f1",
  name: "spec.pdf",
  category: "pdf",
  state: "done",
  reason: null,
  holder: null,
  ...over,
});

test("a done file shows its name and kind and nothing else", () => {
  assert.deepEqual(fileRowOf(file({})), { key: "f1", name: "spec.pdf", format: "PDF" });
  assert.equal(kindOf(file({ category: "markdown" })), "Markdown");
  assert.equal(kindOf(file({ category: "word" })), "Word documents");
});

test("a file that is not done shows its state and the V1 reason words", () => {
  assert.deepEqual(fileRowOf(file({ name: "x.png", category: "images", state: "skipped" })), {
    key: "f1",
    name: "x.png",
    format: "Images",
    tag: "Skipped",
    note: "Skipped: Specflow does not import Images.",
  });
  const failed = fileRowOf(file({ state: "failed", reason: "read_failed" }));
  assert.equal(failed.tag, "Failed");
  assert.equal(failed.note, "Specflow could not read this file.");
  const working = fileRowOf(file({ state: "processing" }));
  assert.equal(working.tag, "Processing");
  assert.equal("note" in working, false, "a file still working has no reason");
});

test("a refused rename says why in words: 422 the name, 409 not an upload, 404 gone, else try again", () => {
  assert.equal(renameProblem(new DataError("validation", false, { apiCode: "validation_failed" })), CANVAS_COPY.renameInvalid);
  assert.equal(renameProblem(new DataError("conflict", false, { apiCode: "source_not_renamable" })), CANVAS_COPY.renameNot);
  assert.equal(renameProblem(new DataError("not_found", false, { apiCode: "source_not_found" })), CANVAS_COPY.renameGone);
  assert.equal(renameProblem(new DataError("not_found", false)), CANVAS_COPY.renameFailed, "an unknown route is not a gone source");
  assert.equal(renameProblem(new DataError("unavailable", true)), CANVAS_COPY.renameFailed);
  assert.equal(renameProblem(new Error("boom")), CANVAS_COPY.renameFailed);
});
