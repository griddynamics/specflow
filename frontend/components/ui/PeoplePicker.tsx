"use client";
// FE-CP-04, FE-DS-02, FE-DOD-05 (R5): port of `PeoplePicker` in bundle.js. Type a name, email or role, pick a suggestion with
// the mouse or ArrowUp/ArrowDown + Enter; picks become removable chips (their x button or Backspace on an empty field).
// Additions over bundle.js: option ids with aria-activedescendant, and Esc closes only the list (stopPropagation) so an
// enclosing Modal stays open.
import { useId, useRef, useState, type KeyboardEvent } from "react";
import { Avatar } from "./Avatar";
import { GlassSurface } from "./GlassSurface";
import { Icon } from "./Icon";
import "./ui-gaps-app.css";
import { filterPeople, type Person } from "./app-logic";
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
  const sugg = filterPeople(people, sel, query);
  const showList = open && sugg.length > 0;

  function add(x: Person) {
    set(sel.concat([x.id]));
    setQuery("");
    setActive(0);
  }
  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive(Math.max(0, Math.min(active + 1, sugg.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(Math.max(active - 1, 0));
    } else if (e.key === "Enter" && open && truthy(sugg[active])) {
      e.preventDefault();
      add(sugg[active]);
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
          const x = people.find((y) => y.id === pid);
          if (!x) return null;
          return (
            <span key={pid} className="gdg-chip" data-testid="people-chip">
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
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
            setActive(0);
          }}
          onFocus={() => {
            setOpen(true);
          }}
          onKeyDown={onKey}
        />
      </div>
      {truthy(help) ? <span className="gdg-field__help">{help}</span> : null}
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
