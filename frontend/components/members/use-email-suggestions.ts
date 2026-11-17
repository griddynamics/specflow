"use client";
// docs/projects/ui.md#general-tab, api.md#suggestions: the people suggested under an email field. From 3 typed characters, after a
// 200 ms pause; an answer older than the latest query is dropped. Shared by Settings (with `projectId`) and Create Project (without).
// The rules live in `createSuggester`, which has no React in it; the hook only wires it to state.
import { useEffect, useMemo, useState } from "react";
import { usePorts } from "@/lib/data/provider";
import { SUGGEST_MIN_CHARS, type Suggestion } from "@/lib/domain/members";

export const SUGGEST_DEBOUNCE_MS = 200;
const NONE: Suggestion[] = [];

export interface Suggester {
  /** The field's text changed: schedules the lookup (or clears the list when the text is too short). */
  query(text: string): void;
  /** Stops the pending timer and ignores any answer still on its way. */
  dispose(): void;
}

/** The trimmed text to look up, or null while it is shorter than the minimum (counted in code points, as the API does). */
export function suggestionQuery(text: string): string | null {
  const q = text.trim();
  return Array.from(q).length >= SUGGEST_MIN_CHARS ? q : null;
}

export function createSuggester(opts: {
  suggest: (q: string) => Promise<Suggestion[]>;
  onItems: (items: Suggestion[]) => void;
  delayMs?: number;
}): Suggester {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let latest = 0; // the id of the newest query; a lookup that finished for an older id is stale
  const cancel = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };
  return {
    query(text) {
      cancel();
      const id = ++latest;
      const q = suggestionQuery(text);
      if (q === null) {
        opts.onItems(NONE);
        return;
      }
      timer = setTimeout(() => {
        timer = undefined;
        opts
          .suggest(q)
          .then((items) => {
            if (id === latest) opts.onItems(items);
          })
          .catch(() => {
            if (id === latest) opts.onItems(NONE); // a failed lookup is no list; the field keeps working
          });
      }, opts.delayMs ?? SUGGEST_DEBOUNCE_MS);
    },
    dispose() {
      cancel();
      latest++;
    },
  };
}

/** The suggestions for `text`; `projectId` excludes that project's members and invited emails (Settings), none for Create Project. */
export function useEmailSuggestions(text: string, projectId?: string): { items: Suggestion[] } {
  const { members } = usePorts();
  const [items, setItems] = useState<Suggestion[]>(NONE);
  const suggester = useMemo(
    () => createSuggester({ suggest: (q) => members.suggest(q, projectId), onItems: setItems }),
    [members, projectId],
  );
  useEffect(() => {
    suggester.query(text);
  }, [suggester, text]);
  useEffect(
    () => () => {
      suggester.dispose();
    },
    [suggester],
  );
  return { items };
}
