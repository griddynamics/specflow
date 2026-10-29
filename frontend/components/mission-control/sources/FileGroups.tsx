"use client";
// FR-SRC-0009, 0011 (mockup "Details 3 — grouped"): the files of a source, flat, in three groups: Needs attention, In progress, Done.
// Each heading is a button that folds its group (aria-expanded) and shows the count of the whole source, also before the files are
// loaded. The next page is asked for when the end of the loaded rows comes into view (an IntersectionObserver on the loading row); the
// observer is made again after every page, so a row that is still in view asks for the next page too. A failed page stops the observer and
// shows "Could not load more files." with a Try again button. The rows are ONE Tab stop (roving focus, arrow keys, Home and End move
// between them), so a long list never sits between the heading and the footer buttons; the focused row shows its whole name.
import { useEffect, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { Source, SourceFile } from "@/lib/domain/sources";
import { GROUPS, groupIdOf, groupTotals, heading, wantsMore, type GroupId } from "./source-logic";
import { SourceFileRow } from "./SourceFileRow";
import { truthy } from "@/components/ui/utils";

/** The row a navigation key moves to (Arrow Up/Down, Home, End), or null for any other key. */
function moveTarget(key: string, at: number, count: number): number | null {
  switch (key) {
    case "ArrowDown":
      return at + 1;
    case "ArrowUp":
      return at - 1;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}

export function FileGroups({
  counts,
  items,
  hasMore,
  loading,
  moreFailed,
  onMore,
  folded,
  onToggle,
}: Readonly<{
  counts: Source["counts"];
  items: SourceFile[];
  hasMore: boolean;
  loading: boolean;
  moreFailed: boolean;
  onMore(): void;
  folded: Record<GroupId, boolean>;
  onToggle(group: GroupId): void;
}>) {
  const total = groupTotals(counts);
  const need = wantsMore(counts, items, hasMore, folded);
  const end = useRef<HTMLDivElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState<string | null>(null);
  const ask = useRef(onMore);
  useEffect(() => {
    ask.current = onMore;
  });

  useEffect(() => {
    const el = end.current;
    if (!need || moreFailed || !el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) ask.current();
      },
      { root: root.current, rootMargin: "120px" },
    );
    io.observe(el);
    return () => {
      io.disconnect();
    };
  }, [need, moreFailed, items.length]);

  const visible = GROUPS.filter((g) => total[g] > 0 && !folded[g]).flatMap((g) => items.filter((f) => groupIdOf(f.state) === g));
  const tabId = visible.some((f) => f.id === active) ? active : (visible[0]?.id ?? null);

  const rowOf = (t: EventTarget): HTMLElement | null => (t instanceof HTMLElement && t.matches("li.mcs-file") ? t : null);
  const onFocus = (e: FocusEvent) => {
    const row = rowOf(e.target);
    if (truthy(row?.dataset.id)) setActive(row.dataset.id);
  };
  const onKey = (e: KeyboardEvent) => {
    if (!rowOf(e.target)) return;
    const rows = Array.from(root.current?.querySelectorAll<HTMLElement>("li.mcs-file") ?? []);
    const at = rows.indexOf(e.target as HTMLElement);
    const to = moveTarget(e.key, at, rows.length);
    if (to === null || at < 0) return;
    e.preventDefault();
    rows[Math.min(Math.max(to, 0), rows.length - 1)]?.focus();
  };

  return (
    <div className="mcs-groups" role="presentation" ref={root} data-testid="sd-groups" onFocus={onFocus} onKeyDown={onKey}>
      {GROUPS.filter((g) => total[g] > 0).map((g) => {
        const rows = items.filter((f) => groupIdOf(f.state) === g);
        const open = !folded[g];
        return (
          <section key={g} className="mcs-group" data-testid="sd-group" data-group={g}>
            <h3 className="mcs-group__head">
              <button
                type="button"
                className="mcs-group__toggle"
                aria-expanded={open}
                onClick={() => {
                  onToggle(g);
                }}
                data-testid="sd-group-toggle"
              >
                <Icon name={open ? "caret-down" : "caret-right"} size={16} />
                <span className="eyebrow">{heading(g, total[g])}</span>
              </button>
            </h3>
            {open && rows.length ? (
              <ul className="mcs-files">
                {rows.map((f) => (
                  <SourceFileRow key={f.id} file={f} tab={f.id === tabId} />
                ))}
              </ul>
            ) : null}
          </section>
        );
      })}
      {need && moreFailed ? (
        <div className="mcs-more mcs-more--failed" role="alert" data-testid="sd-more-failed">
          <span className="caption">Could not load more files.</span>
          <Button variant="glass" size="sm" onClick={onMore} data-testid="sd-more-retry">
            Try again
          </Button>
        </div>
      ) : null}
      {need && !moreFailed ? (
        <div className="caption mcs-more" ref={end} data-testid="sd-more" data-loading={loading || undefined} role="status">
          Loading more files…
        </div>
      ) : null}
    </div>
  );
}
