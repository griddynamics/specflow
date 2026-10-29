// FE-DATA-01: shared domain types (TECH-SPECS §2.2). Pure: no React, no fetch, no fixtures.
// Ids are plain strings, and so are timestamps: ISO 8601, as the API returns them.
export type Unsubscribe = () => void;
export interface DocRef {
  path: string;
  anchor?: string;
  label?: string;
} // MOCKS "path#anchor"
