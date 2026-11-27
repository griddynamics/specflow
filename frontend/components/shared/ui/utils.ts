// FE-DS-02 (R5): `cx` from bundle.js. Server-safe: no hooks here (see use-controlled.ts).

/** Joins the truthy class names, like `cx` in bundle.js. */
export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/** An explicit truthiness test (JS semantics: false, 0, NaN, "", null and undefined are not set); narrows the tested value. */
export function truthy<T>(value: T): value is Exclude<T, false | 0 | "" | null | undefined> {
  return Boolean(value);
}

/** The string, or `undefined` when it is empty or missing: `nonEmpty(text) ?? fallback` keeps the `text || fallback` meaning. */
export function nonEmpty(value: string | null | undefined): string | undefined {
  return value === undefined || value === null || value === "" ? undefined : value;
}

/** `tabIndex` of an element that is a stop of the keyboard order (a scroll region, a roving-tabindex item) or only a focus target (-1). */
export function tabStop(inOrder: boolean): { tabIndex: 0 | -1 } {
  return { tabIndex: inOrder ? 0 : -1 };
}
