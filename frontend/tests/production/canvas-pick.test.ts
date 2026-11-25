// The checklists of Add source (Jira epic issues, Drive files): the shared selection model, the Drive list (which files, the folder after the
// name, the status line), the Drive form's test answer and save body through the production port, the stub's files. Port 3289 (stub API).
import assert from "node:assert/strict";
import { test } from "node:test";
import { folderHint, pickableFiles, selectionText } from "@/components/v2/canvas/drive-pick-logic";
import { checkAll, checkNone, excludeOf, pickedCount, toggle } from "@/components/v2/canvas/pick-logic";
import { createHttpSources, toTest } from "@/lib/data/http/sources";
import { createHttpProjects } from "@/lib/data/http/projects";
import type { DriveFile, DriveTestResult } from "@/lib/domain/sources";
import { serveStub } from "../support/stub-fetch";

type Tested = Extract<DriveTestResult, { ok: true }>;
const file = (n: number, path = "", category: DriveFile["category"] = "pdf"): DriveFile => ({
  id: `f${String(n)}`,
  name: `File ${String(n)}.pdf`,
  path,
  category,
});
const FOUR = [1, 2, 3, 4].map((n) => file(n));
const tested = (files: DriveFile[], skipped: DriveFile["category"][] = []): Tested => ({
  ok: true,
  folderId: "x",
  folderName: "X",
  folderCount: 1,
  fileCount: files.length,
  categories: [
    { category: "pdf", processingClass: "processed", count: files.length },
    ...skipped.map((category) => ({ category, processingClass: "skipped" as const, count: 1 })),
  ],
  files,
  filesCapped: false,
});

test("the selection starts with every row checked, toggles one, checks all or none, and lists the ids left out in order", () => {
  const start = checkAll();
  assert.deepEqual(excludeOf(FOUR, start), []);
  const one = toggle(start, "f2", false);
  assert.deepEqual(excludeOf(FOUR, one), ["f2"]);
  assert.deepEqual(excludeOf(FOUR, toggle(one, "f2", true)), []);
  assert.deepEqual(excludeOf(FOUR, toggle(toggle(start, "f4", false), "f1", false)), ["f1", "f4"]);
  assert.deepEqual(excludeOf(FOUR, checkNone(FOUR)), ["f1", "f2", "f3", "f4"]);
  assert.equal(pickedCount(FOUR, one), 3);
  assert.equal(pickedCount(FOUR, checkNone(FOUR)), 0);
  assert.equal(start.size, 0, "toggling never changes the set it was given");
});

test("the folder after a file name is its path, and nothing for a file in the root", () => {
  assert.equal(folderHint(file(1)), undefined);
  assert.equal(folderHint(file(1, "design")), "design");
  assert.equal(folderHint(file(1, "design/archive")), "design/archive");
});

test("the Drive list leaves out the files of a category the import skips", () => {
  const files = [file(1), file(2, "", "shortcuts"), file(3, "a/b"), file(4, "", "other_google")];
  assert.deepEqual(
    pickableFiles(tested(files, ["shortcuts", "other_google"])).map((f) => f.id),
    ["f1", "f3"],
  );
  assert.equal(pickableFiles(tested(files)).length, 4, "stored-only and processed files are listed");
});

test("the Drive status line says N of M files, or No files with nothing checked", () => {
  assert.equal(selectionText(FOUR, checkAll()), "4 of 4 files");
  assert.equal(selectionText(FOUR, toggle(checkAll(), "f3", false)), "3 of 4 files");
  assert.equal(selectionText(FOUR, checkNone(FOUR)), "No files");
  assert.equal(selectionText([file(1)], checkAll()), "1 of 1 file");
});

const ctx = serveStub(3289, () => undefined);
const { sent } = ctx;
const sources = createHttpSources({ changed: () => undefined });
const FOLDER = (id: string) => `https://drive.google.com/drive/folders/${id}`;

async function project() {
  const p = await createHttpProjects().createProject.create({ name: "P" });
  sent.length = 0;
  return p.id;
}

test("the Drive test maps the files (id, name, path, category) in the API's order and the cap flag", async () => {
  const pid = await project();
  const r = await sources.testDrive(pid, FOLDER("fake-synthetic-30"));
  assert.ok(r.ok);
  assert.equal(r.files.length, 11);
  assert.equal(r.filesCapped, false);
  assert.deepEqual(r.files[0], { id: "fake-synthetic-30-f3", name: "Contract.pdf", path: "", category: "pdf" });
  assert.deepEqual(
    r.files.map((f) => f.path),
    r.files.map((f) => f.path).toSorted((a, b) => a.localeCompare(b)),
    "ordered by path",
  );
  assert.ok(r.files.some((f) => f.path === "design/archive"));
  const capped = await sources.testDrive(pid, FOLDER("fake-capped-folder"));
  assert.ok(capped.ok);
  assert.deepEqual([capped.files.length, capped.filesCapped], [1000, true]);
  const empty = await sources.testDrive(pid, FOLDER("fake-empty-folder"));
  assert.ok(empty.ok);
  assert.deepEqual(empty.files, []);
});

test("a test answer without the files, or with a file of the wrong shape, is unknown_error", () => {
  const wire = {
    result: "ok",
    folder: { id: "x", name: "X" },
    folder_count: 1,
    file_count: 1,
    categories: [],
    files: [{ id: "a", name: "A", path: "", category: "pdf" }],
    files_capped: false,
  };
  assert.equal(toTest(wire).ok, true);
  assert.deepEqual(toTest({ ...wire, files: undefined }), { ok: false, reason: "unknown_error" });
  assert.deepEqual(toTest({ ...wire, files_capped: "no" }), { ok: false, reason: "unknown_error" });
  assert.deepEqual(toTest({ ...wire, files: [{ id: "a", name: "A", path: "", category: "nope" }] }), {
    ok: false,
    reason: "unknown_error",
  });
});

test("save sends the ids left out as exclude, and no exclude when every file is picked; a bad id is refused", async () => {
  const pid = await project();
  const id = (n: string) => `fake-import-${n}-f2`;
  const saved = await sources.saveDrive(pid, FOLDER("fake-import-one"), [id("one"), "fake-import-one-f5"]);
  assert.ok(saved.ok);
  const last = () => sent[sent.length - 1];
  assert.equal(last().path, `/api/backend/projects/${pid}/sources/gdrive`);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: FOLDER("fake-import-one"), exclude: [id("one"), "fake-import-one-f5"] });
  const [row] = ctx.api.sources.get(pid) as { _imports: { id: string }[] }[];
  assert.equal(row._imports.length, 8, "11 listed, 1 skipped (a shortcut), 2 left out");
  assert.ok(row._imports.every((f) => f.id !== id("one") && f.id !== "fake-import-one-f5"));
  await sources.saveDrive(pid, FOLDER("fake-import-two"), []);
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: FOLDER("fake-import-two") });
  await sources.saveDrive(pid, FOLDER("fake-import-three"));
  assert.deepEqual(JSON.parse(last().body ?? "{}"), { link: FOLDER("fake-import-three") });
  assert.deepEqual(await sources.saveDrive(pid, FOLDER("fake-import-four"), ["nope"]), { ok: false, reason: "unknown_error" });
});
