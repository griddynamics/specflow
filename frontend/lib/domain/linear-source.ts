// plans/LINEAR/LINEAR-PLAN.md: a Linear project or issue read as a source (one document per issue). FOUNDATION STUB: stream S4 extends this
// file and its adapter. Screens reach it only through `LinearSourcePort`; the added source itself comes back in the sources status.

/** What the check of a Linear link or identifier found, nothing stored. */
export interface LinearSourceCheck {
  kind: "project" | "issue";
  name: string;
  /** The issues an import would take (at most the cap); `capped` = there are more. */
  issues: number;
  capped: boolean;
}

export interface LinearSourcePort {
  check(projectId: string, linkOrIdentifier: string): Promise<LinearSourceCheck>;
  /** Adds it as a source (one per link) and starts its import, once. `exclude` are the identifiers not to import. */
  add(projectId: string, linkOrIdentifier: string, exclude?: readonly string[]): Promise<void>;
}
