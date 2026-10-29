"use client";
// FE-DS-02 (R5): port of `SearchField` in bundle.js.
import { useRef } from "react";
import { Icon } from "./Icon";
import { cx, nonEmpty } from "./utils";
import { useControlled } from "./use-controlled";

/**
 * The one matcher of client-side search: `query` against `haystack`, ignoring letter case, anywhere in the text or only at its start.
 * Diacritics are not folded (the matching of this app, unchanged).
 */
export function matchesQuery(haystack: string, query: string, at: "anywhere" | "start" = "anywhere"): boolean {
  const text = haystack.toLowerCase();
  const wanted = query.toLowerCase();
  return at === "start" ? text.startsWith(wanted) : text.includes(wanted);
}

export interface SearchFieldProps {
  value?: string;
  defaultValue?: string;
  onChange?: (v: string) => void;
  placeholder?: string;
  label?: string;
  className?: string;
}

export function SearchField({ value, defaultValue = "", onChange, placeholder, label, className }: Readonly<SearchFieldProps>) {
  const [v, set] = useControlled(value, defaultValue, onChange);
  const input = useRef<HTMLInputElement>(null);
  return (
    <label className={cx("gdg-search", className)}>
      <Icon name="magnifying-glass" size={18} />
      <input
        ref={input}
        type="search"
        value={v}
        placeholder={nonEmpty(placeholder) ?? "Search"}
        aria-label={nonEmpty(label) ?? nonEmpty(placeholder) ?? "Search"}
        onChange={(e) => {
          set(e.target.value);
        }}
      />
      {v ? (
        <button
          type="button"
          className="gdg-search__clear"
          aria-label="Clear search"
          onClick={() => {
            set("");
            input.current?.focus(); // the clear button unmounts: keep the focus in the field
          }}
        >
          <Icon name="x" size={14} />
        </button>
      ) : null}
    </label>
  );
}
