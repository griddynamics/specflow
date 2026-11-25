// plans/LINEAR/LINEAR-PLAN.md: a Linear project or issue read as a source (one document per issue). Screens reach it only through
// `LinearSourcePort`; the added source itself comes back in the sources status.

/** One issue an import would take: its identifier (`GRA-12`), title and state name (shown, never stored by the UI) and its parent's identifier. */
export interface LinearSourceItem {
  identifier: string;
  title: string;
  stateName: string;
  parent: string | null;
}

/** What the check of a Linear link or identifier found, nothing stored. */
export interface LinearSourceCheck {
  kind: "project" | "issue";
  name: string;
  /** The issues an import would take (at most the cap); `capped` = there are more. */
  issues: number;
  /** The issues in order, at most the cap: the rows the person can leave out. For an issue it is its sub-issues; the issue itself is always imported. */
  items: LinearSourceItem[];
  capped: boolean;
  /** The project's documents, imported with the issues (0 for an issue). */
  documents: number;
}

export interface LinearSourcePort {
  /** Reads a Linear project or issue link, or an identifier such as `GRA-12`; stores nothing. A refusal rejects a `DataError` whose
   * `detail.apiCode` is the closed code (`linear_link_invalid`, `linear_not_connected`, ...). */
  check(projectId: string, linkOrIdentifier: string): Promise<LinearSourceCheck>;
  /** Adds it as a source and starts its import, once (adding the same link again is a new source). `exclude` are the identifiers not to import. */
  add(projectId: string, linkOrIdentifier: string, exclude?: readonly string[]): Promise<void>;
}
