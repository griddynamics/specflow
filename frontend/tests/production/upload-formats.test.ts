// The accepted upload types (lib/domain/upload-formats.ts): the table equals the API's, the canonical
// Content-Type comes from the extension, the picker/texts follow the N2 caps, splitUpload keeps the accepted files only, the
// 413 file_too_large mapping carries the per-type cap, and the toast names the files over the limit. Pure: no server.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ApiError } from "@/lib/api";
import { codeOfStatus, toDataError } from "@/lib/data/http/errors";
import {
  ACCEPTED_EXTENSIONS,
  contentTypeFor,
  FORMAT_LABELS,
  formatOf,
  limitsText,
  sizeText,
  UPLOAD_ACCEPT,
  UPLOAD_FORMATS,
} from "@/lib/domain/upload-formats";
import { splitUpload, toastFor, unsupportedFilesToast } from "@/components/mission-control/sources/source-logic";
import type { UploadReport } from "@/lib/domain/sources";
import { UPLOAD_TABLE } from "../support/stub-api.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const CONTRACT = path.resolve(here, "../../../backend/src/api/fixtures/upload-formats.json");
const MB = 1024 * 1024;

test("the table equals the API contract backend/src/api/fixtures/upload-formats.json (a missing file fails; the backend test keeps it equal to the Python table)", () => {
  const contract = JSON.parse(readFileSync(CONTRACT, "utf8")) as {
    formats: { extension: string; media_type: string; max_bytes: number }[];
    legacy_extensions: string[];
  };
  assert.equal(contract.formats.length, 7);
  assert.deepEqual(
    UPLOAD_FORMATS.map((f) => ({ extension: f.extension, media_type: f.mediaType, max_bytes: f.maxBytes })),
    contract.formats,
  );
  assert.deepEqual(contract.legacy_extensions, [".doc", ".ppt", ".xls"]);
  for (const ext of contract.legacy_extensions) assert.equal(formatOf(`old${ext}`), null, ext);
  assert.deepEqual(
    Object.entries(UPLOAD_TABLE).map(([ext, s]) => [ext, s.type, s.cap]),
    UPLOAD_FORMATS.map((f) => [f.extension, f.mediaType, f.maxBytes]),
    "the stub API's table",
  );
});

test("canonical Content-Type by extension: case-insensitive, a path is fine, browser quirks never matter", () => {
  assert.equal(contentTypeFor("a.pdf"), "application/pdf");
  assert.equal(contentTypeFor("Report.DOCX"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document");
  assert.equal(contentTypeFor("deck.pptx"), "application/vnd.openxmlformats-officedocument.presentationml.presentation");
  assert.equal(contentTypeFor("sheet.xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
  assert.equal(contentTypeFor("page.html"), "text/html");
  assert.equal(contentTypeFor("README.md"), "text/markdown"); // the browser says ""
  assert.equal(contentTypeFor("data.csv"), "text/csv"); // Windows says application/vnd.ms-excel
  assert.equal(contentTypeFor("C:\\docs\\a.b.md"), "text/markdown");
  assert.equal(contentTypeFor("dir/notes.CSV"), "text/csv");
});

test("legacy, unknown and extension-less names are not accepted and get a generic type (the API answers 415)", () => {
  for (const name of ["old.doc", "old.ppt", "old.xls", "notes.txt", "page.htm", "archive", ".md", "a.pdf.exe", "a.docm"]) {
    assert.equal(formatOf(name), null, name);
    assert.equal(contentTypeFor(name), "application/octet-stream", name);
  }
});

test("picker accept list, texts and caps agree with N2", () => {
  assert.equal(
    UPLOAD_ACCEPT,
    [
      ".pdf,application/pdf",
      ".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      ".pptx,application/vnd.openxmlformats-officedocument.presentationml.presentation",
      ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      ".html,text/html",
      ".md,text/markdown",
      ".csv,text/csv",
    ].join(","),
  );
  assert.equal(ACCEPTED_EXTENSIONS, ".pdf, .docx, .pptx, .xlsx, .html, .md, .csv");
  assert.equal(FORMAT_LABELS, "PDF, Word, PowerPoint, Excel, HTML, Markdown or CSV");
  assert.equal(limitsText(), "PDF up to 100 MB; Word, PowerPoint and Excel up to 50 MB; HTML, Markdown and CSV up to 10 MB");
  assert.equal(sizeText(100 * MB), "100 MB");
  assert.equal(sizeText(1.5 * MB), "1.5 MB");
});

test("splitUpload keeps the accepted types (by extension, whatever file.type says) and names the rest", () => {
  const f = (name: string, type: string) => new File(["x"], name, { type });
  const picked = [
    f("a.pdf", "application/pdf"),
    f("b.md", ""),
    f("c.csv", "application/vnd.ms-excel"),
    f("d.doc", "application/msword"),
    f("e.xls", "application/vnd.ms-excel"),
    f("f.txt", "text/plain"),
    f("g.pdf", ""),
  ];
  const { files, skipped } = splitUpload(picked);
  assert.deepEqual(
    files.map((x) => x.name),
    ["a.pdf", "b.md", "c.csv", "g.pdf"],
  );
  assert.deepEqual(skipped, ["d.doc", "e.xls", "f.txt"]);
  assert.deepEqual(splitUpload([]), { files: [], skipped: [] });
});

test("the refusal toast names the files and the accepted types", () => {
  const one = unsupportedFilesToast(["old.doc"]);
  assert.equal(one.tone, "danger");
  assert.equal(one.title, "File type not supported");
  assert.equal(
    one.message,
    "old.doc was not added. Specflow reads PDF, Word, PowerPoint, Excel, HTML, Markdown or CSV files (.pdf, .docx, .pptx, .xlsx, .html, .md, .csv).",
  );
  assert.match(unsupportedFilesToast(["a.doc", "b.ppt"]).message, /^a\.doc, b\.ppt were not added\./);
});

test("413 file_too_large: by code and by status; the cap of the type comes from details.max_bytes; no server text", () => {
  assert.equal(codeOfStatus(413), "file_too_large");
  const withCap = toDataError(new ApiError(413, "file_too_large", "file_too_large", 10 * MB));
  assert.deepEqual(
    [withCap.code, withCap.retryable, withCap.detail],
    ["file_too_large", false, { status: 413, apiCode: "file_too_large", maxBytes: 10 * MB }],
  );
  const ingress = toDataError(new ApiError(413, "http_413", "Request failed (HTTP 413). Please try again."));
  assert.deepEqual([ingress.code, ingress.detail?.maxBytes], ["file_too_large", undefined]);
  assert.equal(withCap.message, "file_too_large");
});

const report = (over: Partial<UploadReport>): UploadReport => ({
  sourceId: "s",
  picked: 0,
  added: 0,
  alreadyInProject: 0,
  readAgain: 0,
  duplicates: 0,
  notAdded: 0,
  ...over,
});

test("the upload toast names a file over its cap with that cap; other refusals are counted apart", () => {
  const t = toastFor(
    report({
      picked: 4,
      added: 1,
      notAdded: 3,
      tooLarge: [
        { name: "big.csv", maxBytes: 10 * MB },
        { name: "big.md", maxBytes: 10 * MB },
      ],
    }),
  );
  assert.equal(t.tone, "danger");
  assert.equal(t.message, "Specflow collects them now. big.csv, big.md are over the 10 MB limit. 1 file was not added.");
  const pdf = toastFor(report({ picked: 1, notAdded: 1, tooLarge: [{ name: "huge.pdf", maxBytes: 100 * MB }] }));
  assert.equal(pdf.title, "Files not added");
  assert.equal(pdf.message, "huge.pdf is over the 100 MB limit.");
  const two = toastFor(
    report({
      picked: 2,
      notAdded: 2,
      tooLarge: [
        { name: "a.pdf", maxBytes: 100 * MB },
        { name: "b.docx", maxBytes: 50 * MB },
      ],
    }),
  );
  assert.equal(two.message, "a.pdf is over the 100 MB limit. b.docx is over the 50 MB limit.");
  assert.equal(toastFor(report({ picked: 1, notAdded: 1 })).message, "1 file was not added.");
});
