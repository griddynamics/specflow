"use client";
// FE-CP-04, FE-DS-02, FE-DOD-05 (R5): port of `PeoplePicker` in bundle.js. Type a name, email or role, pick a suggestion with
// the mouse or ArrowUp/ArrowDown + Enter; picks become removable chips (their x button or Backspace on an empty field).
// Additions over bundle.js: option ids with aria-activedescendant, and Esc closes only the list (stopPropagation) so an
// enclosing Modal stays open.
// Optional email mode (Create Project, docs/projects/ui.md#create-project-modal): `allowTyped` lets Enter or a comma commit a typed
// email as a chip (Person id = the normalised email), `watchQuery` feeds async suggestions (`people` then holds the answer and
// is not filtered again), `chipErrors`/`error` mark chips and the field. Without these props the picker behaves as before.
import { useEffect, useId, useImperativeHandle, useRef, useState, type KeyboardEvent, type Ref } from "react";
import { Avatar } from "./Avatar";
import { GlassSurface } from "./GlassSurface";
import { Icon } from "./Icon";
import "./ui-gaps-app.css";
import { commitTyped, filterPeople, type Person } from "./app-logic";
import { useControlled } from "./use-controlled";
import { useOutside } from "./use-outside";
import { nonEmpty, truthy } from "./utils";

export interface PeoplePickerProps {
  people: Person[];
  value?: string[];
  defaultValue?: string[];
  onChange?: (ids: string[]) => void;
  label?: string;
  help?: string;
  placeholder?: string;
  defaultOpen?: boolean;
  /** Enter or comma commits the typed text as an email chip (id = normalised email). */
  allowTyped?: boolean;
  /** At most this many chips; a typed or picked email beyond it is refused. */
  max?: number;
  /** Called with the field text on every change: the caller loads suggestions and passes them as `people`. */
  watchQuery?: (query: string) => void;
  /** Typed text refused: not an email, already a chip, or the list is full. */
  whenRejected?: (reason: "invalid" | "duplicate" | "full", text: string) => void;
  /** Message under the field (replaces help; turns the field danger). */
  error?: string;
  /** Messages by chip id: the chip is marked and the message shows under the field. */
  chipErrors?: Record<string, string>;
  /** Lets the owner commit the typed text on demand (Create clicked with an email still in the field). */
  commitRef?: Ref<PickerCommit>;
}

/** The result of committing the typed text: nothing typed, refused (`whenRejected` was called, the text stays), or added (`ids` = all chips now). */
export type CommitResult = { kind: "empty" | "refused" } | { kind: "added"; ids: string[] };
export interface PickerCommit {
  commit: () => CommitResult;
}

const avatarKind = (p: Person) => (p.kind === "bot" ? ("bot" as const) : undefined);

export function PeoplePicker({
  people,
  value,
  defaultValue,
  onChange,
  label,
  help,
  placeholder,
  defaultOpen,
  allowTyped,
  max,
  watchQuery,
  whenRejected,
  error,
  chipErrors,
  commitRef,
}: Readonly<PeoplePickerProps>) {
  const id = useId();
  const [sel, set] = useControlled<string[]>(value, defaultValue ?? [], onChange);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(defaultOpen === true);
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  useOutside(ref, open, () => {
    setOpen(false);
  });
  const [picked, setPicked] = useState<ReadonlyMap<string, Person>>(new Map()); // people picked from the list: a chip keeps its name after an async list is cleared
  const [navigated, setNavigated] = useState(false); // an arrow key moved onto a suggestion: Enter then picks it, not the typed text
  const asyncList = watchQuery !== undefined;
  useEffect(() => {
    watchQuery?.(query); // tells the owner the text after it rendered (also "" at mount and after a commit); keep the callback stable
  }, [watchQuery, query]);
  const sugg = asyncList ? people.filter((x) => !sel.includes(x.id)) : filterPeople(people, sel, query);
  const showList = open && sugg.length > 0;

  function changeQuery(q: string) {
    setQuery(q);
    setActive(0);
    setNavigated(false);
  }
  function add(x: Person) {
    if (max !== undefined && sel.length >= max) {
      whenRejected?.("full", x.id);
      return;
    }
    setPicked((m) => new Map(m).set(x.id, x));
    set(sel.concat([x.id]));
    changeQuery("");
  }
  /** Commits the typed text as a chip; false when there was nothing to commit or it was refused. */
  function commitText(): boolean {
    const c = commitTyped(sel, query, max);
    if (c.kind === "empty") return false;
    if (c.kind !== "add") {
      whenRejected?.(c.kind, query.trim());
      return true; // handled: the text stays so it can be fixed
    }
    set(sel.concat([c.email]));
    changeQuery("");
    return true;
  }
  useImperativeHandle(commitRef, () => ({
    commit(): CommitResult {
      const c = commitTyped(sel, query, max);
      if (c.kind === "empty") return { kind: "empty" };
      if (c.kind !== "add") {
        whenRejected?.(c.kind, query.trim());
        return { kind: "refused" };
      }
      const ids = sel.concat([c.email]);
      set(ids);
      changeQuery("");
      return { kind: "added", ids };
    },
  }));
  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setNavigated(true);
      setActive(Math.max(0, Math.min(active + 1, sugg.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setNavigated(true);
      setActive(Math.max(active - 1, 0));
    } else if (e.key === "Enter" && open && truthy(sugg[active]) && (allowTyped !== true || navigated)) {
      e.preventDefault();
      add(sugg[active]);
    } else if (allowTyped === true && (e.key === "Enter" || e.key === ",") && !e.nativeEvent.isComposing) {
      if (commitText() || e.key === ",") e.preventDefault(); // a comma is never typed into the field
    } else if (e.key === "Escape" && showList) {
      // Closes the list only. Next hydrates on `document`, where a Modal also listens, so stopPropagation is not enough:
      e.nativeEvent.stopImmediatePropagation(); // React's root listener was registered first, so it runs first
      setOpen(false);
    } else if (e.key === "Backspace" && !query && sel.length) {
      set(sel.slice(0, -1));
    }
  }

  return (
    <div
      role="presentation"
      className="gdg-field gdg-pp"
      ref={ref}
      onBlur={(e) => {
        // focus left the component (Tab away): close the list
        if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false);
      }}
    >
      {truthy(label) ? (
        <label className="gdg-field__label" htmlFor={id}>
          {label}
        </label>
      ) : null}
      {/* The box only forwards a click to the input; the keyboard uses the input itself. */}
      <div
        className="gdg-pp__box"
        role="presentation"
        onClick={() => {
          inputRef.current?.focus();
        }}
      >
        {sel.map((pid) => {
          const x = picked.get(pid) ?? people.find((y) => y.id === pid) ?? (allowTyped === true ? { id: pid, name: pid } : undefined);
          if (!x) return null;
          const bad = chipErrors?.[pid];
          return (
            <span
              key={pid}
              className={`gdg-chip${truthy(bad) ? " gdg-chip--error" : ""}`}
              data-testid="people-chip"
              data-invalid={truthy(bad) ? "true" : undefined}
            >
              <Avatar name={x.name} size="sm" title={false} kind={avatarKind(x)} />
              {x.name}
              <button
                type="button"
                aria-label={`Remove ${x.name}`}
                onClick={(e) => {
                  e.stopPropagation();
                  set(sel.filter((y) => y !== pid));
                }}
              >
                <Icon name="x" size={12} />
              </button>
            </span>
          );
        })}
        <input
          ref={inputRef}
          id={id}
          value={query}
          placeholder={sel.length ? "" : (nonEmpty(placeholder) ?? "Add people by name or email")}
          role="combobox"
          aria-expanded={showList}
          aria-controls={showList ? `${id}-l` : undefined}
          aria-activedescendant={showList ? `${id}-o${String(active)}` : undefined}
          aria-autocomplete="list"
          autoComplete="off"
          aria-invalid={truthy(error) || Object.keys(chipErrors ?? {}).length > 0 ? true : undefined}
          onChange={(e) => {
            changeQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => {
            setOpen(true);
          }}
          onKeyDown={onKey}
        />
      </div>
      {truthy(error) ? (
        <span className="gdg-field__help gdg-pp__error" role="alert">
          {error}
        </span>
      ) : null}
      {Object.entries(chipErrors ?? {}).map(([pid, msg]) => (
        <span key={pid} className="gdg-field__help gdg-pp__error" role="alert">
          {pid}: {msg}
        </span>
      ))}
      {truthy(help) && !truthy(error) ? <span className="gdg-field__help">{help}</span> : null}
      {showList ? (
        <GlassSurface variant="strong" floating className="gdg-pp__list" role="listbox" id={`${id}-l`}>
          {sugg.map((x, i) => (
            <div
              key={x.id}
              id={`${id}-o${String(i)}`}
              role="option"
              tabIndex={-1}
              aria-selected={i === active}
              className={`gdg-pp__opt${i === active ? " is-active" : ""}`}
              onMouseDown={(e) => {
                e.preventDefault();
                add(x);
              }}
              onMouseEnter={() => {
                setActive(i);
              }}
            >
              <Avatar name={x.name} size="md" title={false} kind={avatarKind(x)} />
              <span>
                <b>{x.name}</b>
                <small>{[x.role, x.email].filter(Boolean).join(" · ")}</small>
              </span>
            </div>
          ))}
        </GlassSurface>
      ) : null}
    </div>
  );
}
