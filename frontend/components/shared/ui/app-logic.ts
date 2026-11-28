// FE-DS-02, FE-DOD-05 (P05, R5): the pure parts of the app components, split out so they run without a browser.
// Ports of the helper functions in bundle.js part 3. No hooks, no DOM, server-safe.

// ---- PeoplePicker typed emails (docs/projects/ui.md#create-project-modal) ------------------------------------------------------

/** The local format check (the API has the last word): one `@`, text on both sides, a dot in the domain, no spaces. */
export function isEmailShape(raw: string): boolean {
  return /^[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+$/.test(raw.trim());
}
