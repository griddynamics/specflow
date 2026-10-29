// ruleid: production-no-test-justification-comment
// Exported so the unit tests can reach the parser.
export const parse = (text: string): string => text.trim();

// ok: production-no-test-justification-comment
// Trims the surrounding whitespace a pasted value carries.
export const clean = (text: string): string => text.trim();

// ruleid: production-no-test-justification-comment
/* Stubbed in jsdom, so guard it. */
export const scroll = (element: HTMLElement): void => element.scrollIntoView();
