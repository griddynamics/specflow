"use client";
// FE-REV-02, FE-DS-02, FE-DOD-05 (R5): port of `MentionInput` in bundle.js. A textarea where "@" opens a list of people (and
// the bot); ArrowUp/Down move, Enter or Tab insert "@handle ", Esc closes the list only (stopPropagation), Cmd/Ctrl+Enter
// submits. The caret goes back after the inserted mention in an effect (no timers).
import {
  useEffect,
  useId,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type Ref,
} from "react";
import { Avatar } from "./Avatar";
import { GlassSurface } from "./GlassSurface";
import { applyMention, filterMentions, mentionQuery, type Person } from "./app-logic";
import { useControlled } from "./use-controlled";
import { cx, nonEmpty, truthy } from "./utils";

export interface MentionInputProps {
  people: Person[];
  value?: string;
  defaultValue?: string;
  onChange?: (v: string) => void;
  onSubmit?: (v: string) => void;
  placeholder?: string;
  rows?: number;
  label?: string;
  /** Focus the box on mount (an empty, focused comment box after "Comment", FE-REV-03.AC1). Off by default. */
  autoFocus?: boolean;
  /** Ref to the textarea. */
  inputRef?: Ref<HTMLTextAreaElement>;
}

export function MentionInput({
  people,
  value,
  defaultValue,
  onChange,
  onSubmit,
  placeholder,
  rows,
  label,
  autoFocus,
  inputRef,
}: Readonly<MentionInputProps>) {
  const id = useId();
  const ref = useRef<HTMLTextAreaElement>(null);
  const [val, set] = useControlled<string>(value, defaultValue ?? "", onChange);
  const [query, setQuery] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  // lets the owner focus or read the box
  useImperativeHandle(inputRef, () => {
    const box = ref.current;
    if (box === null) throw new Error("MentionInput: the textarea is not mounted");
    return box;
  });
  const caret = useRef<number | null>(null); // set by pick(); applied once the new text is rendered
  const sugg = filterMentions(people, query);
  const open = sugg.length > 0;

  // P15 (QA M1): the list opens on the side with more room INSIDE the nearest scrolling ancestor (the review panel clips anything that leaves it),
  // and is as tall as that room allows (it scrolls itself); bundle.js always opens it upward.
  const wrap = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<{ below: boolean; max: number } | null>(null);
  useLayoutEffect(() => {
    const box = ref.current;
    if (!open || !box || !wrap.current) {
      setPlace(null);
      return;
    }
    let host: HTMLElement | null = wrap.current.parentElement;
    while (host && !/auto|scroll|hidden|clip/.test(getComputedStyle(host).overflowY)) host = host.parentElement;
    const r = box.getBoundingClientRect();
    const h = host ? host.getBoundingClientRect() : null;
    const hostTop = h?.top ?? 0;
    const hostBottom = h?.bottom ?? window.innerHeight;
    const gap = 6 + 8; // the list offset and a margin to the edge
    const above = r.top - Math.max(hostTop, 0) - gap;
    const below = Math.min(hostBottom, window.innerHeight) - r.bottom - gap;
    setPlace(below >= above ? { below: true, max: below } : { below: false, max: above });
  }, [open, sugg.length]);

  // Runs after every render, so a caret set by pick() is applied once and never kept for a later render.
  useEffect(() => {
    const el = ref.current;
    if (caret.current == null || !el) return;
    el.focus();
    el.setSelectionRange(caret.current, caret.current);
    caret.current = null;
  });

  function onInput(e: ChangeEvent<HTMLTextAreaElement>) {
    const v = e.target.value;
    set(v);
    setQuery(mentionQuery(v, e.target.selectionStart));
    setActive(0);
  }
  function pick(x: Person) {
    const el = ref.current;
    if (!el) return;
    const next = applyMention(val, el.selectionStart, x);
    set(next.text);
    setQuery(null);
    caret.current = next.caret;
  }
  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing) return; // Enter that confirms an IME candidate must not pick or send
    if (open) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((active + 1) % sugg.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((active - 1 + sugg.length) % sugg.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        pick(sugg[active]);
        return;
      }
      if (e.key === "Escape") {
        // Closes the list only. Next hydrates on `document`, where a Modal also listens, so stopPropagation is not enough:
        e.nativeEvent.stopImmediatePropagation(); // React's root listener was registered first, so it runs first
        setQuery(null);
        return;
      }
    }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && onSubmit) {
      e.preventDefault();
      onSubmit(val);
    }
  }

  return (
    <div className="gdg-mention-input" ref={wrap}>
      <textarea
        ref={ref}
        autoFocus={autoFocus}
        rows={rows ?? 2}
        value={val}
        placeholder={nonEmpty(placeholder) ?? "Reply… type @ to mention"}
        aria-label={nonEmpty(label) ?? "Comment"}
        aria-autocomplete="list"
        aria-controls={open ? `${id}-l` : undefined}
        aria-activedescendant={open ? `${id}-o${String(active)}` : undefined}
        onChange={onInput}
        onKeyDown={onKey}
      />
      {open ? (
        <GlassSurface
          variant="strong"
          floating
          className={cx("gdg-pp__list gdg-mention__list", truthy(place?.below) && "is-below")}
          style={place ? { maxHeight: place.max } : undefined}
          role="listbox"
          id={`${id}-l`}
        >
          {sugg.map((x, i) => (
            <div
              key={x.id || x.name}
              id={`${id}-o${String(i)}`}
              role="option"
              tabIndex={-1}
              aria-selected={i === active}
              className={cx("gdg-pp__opt", i === active && "is-active")}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(x);
              }}
            >
              <Avatar name={x.name} kind={x.kind === "bot" ? "bot" : undefined} size="sm" title={false} />
              <span>
                <b>{x.name}</b>
                <small>
                  {[truthy(x.handle) ? `@${x.handle}` : "", nonEmpty(x.role) ?? nonEmpty(x.email) ?? ""].filter(Boolean).join(" · ")}
                </small>
              </span>
            </div>
          ))}
        </GlassSurface>
      ) : null}
    </div>
  );
}
