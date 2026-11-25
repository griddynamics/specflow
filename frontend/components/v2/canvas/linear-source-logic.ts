// The rules of the Linear project source, free of React. FOUNDATION STUB: stream S4 adds the check text and the selection helpers here.

/** What a Linear project node says when the API reports its import failed (`no_access`, or any other closed reason). */
export const LINEAR_SOURCE_WORDS = {
  noAccessMeta: "Cannot read it",
  noAccess: "The Linear account that connected it cannot read this project now. Another member reconnects Linear.",
  failedMeta: "Import failed",
  failed: "The import failed. Add the project again.",
} as const;
