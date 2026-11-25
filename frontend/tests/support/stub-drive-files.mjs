// The files a stubbed Drive folder lists on a test (backend contract of POST .../sources/gdrive/test): `{id, name, path, category}` ordered by
// path then name, `path` the folder names from the shared root (root left out) joined with "/", "" for a file in the root. The default folder
// (11 files in 3 folders) has root files, a nested folder and a file of a skipped category (a shortcut). "fake-capped-folder" lists the cap.
const FILES_CAP = 1000;

const DEFAULT = [
  ["Roadmap", "", "google_docs"],
  ["Meeting notes", "", "google_docs"],
  ["Contract.pdf", "", "pdf"],
  ["Onboarding", "design", "google_docs"],
  ["Specs.pdf", "design", "pdf"],
  ["Brief.docx", "design", "word"],
  ["Glossary", "design/archive", "google_docs"],
  ["Pricing.pdf", "design/archive", "pdf"],
  ["Release plan", "design/archive", "google_docs"],
  ["Letter.docx", "notes", "word"],
  ["Link to Roadmap", "notes", "shortcuts"],
];

const byPathThenName = (a, b) => a.path.localeCompare(b.path) || a.name.localeCompare(b.name);

/** The listed files of a folder id, in the API's order, with stable ids (`<folder>-<n>`). */
export function driveFilesOf(folderId) {
  const rows =
    folderId === "fake-capped-folder"
      ? Array.from({ length: FILES_CAP }, (_, i) => [`Report ${String(i + 1).padStart(4, "0")}.pdf`, i % 3 === 0 ? "" : "reports", "pdf"])
      : folderId === "fake-empty-folder" || folderId === "fake-forms-only"
        ? []
        : DEFAULT;
  return rows.map(([name, path, category], i) => ({ id: `${folderId}-f${String(i + 1)}`, name, path, category })).sort(byPathThenName);
}

/** The folder is capped when the stub lists the cap and more files are behind it. */
export const driveFilesCapped = (folderId) => folderId === "fake-capped-folder";
