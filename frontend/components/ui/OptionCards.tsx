"use client";
// FE-DEC-02, FE-DS-02, FE-DOD-05 (R5): port of `OptionCards` in bundle.js. A radiogroup of lettered cards (A, B, C ...), an
// optional "Other" card (value "__other") that reveals a text box, or a Yes/No pair. One tab stop (the selected card, else
// the first); arrow keys move AND select, like native radios. `defaultOther` is used by the bundle preview, not in
// index.d.ts. Added: choosing "Other" with a click moves the focus into the box.
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Icon } from "./Icon";
import { OPTION_LETTERS, radioMove } from "./app-logic";
import { useControlled } from "./use-controlled";
import { cx, nonEmpty, truthy } from "./utils";

export const OTHER_VALUE = "__other";

export interface OptionCardsProps {
  type?: "choice" | "yesno";
  options?: { value: string; label: string; description?: string }[];
  /** Adds a lettered "Other" card with a text box (value "__other"). */
  allowOther?: boolean;
  otherLabel?: string;
  otherPlaceholder?: string;
  onOtherChange?: (text: string) => void;
  /** Initial text of the "Other" box. */
  defaultOther?: string;
  value?: string;
  defaultValue?: string;
  onChange?: (v: string) => void;
  yesLabel?: string;
  noLabel?: string;
  /** Read-only: no card can be chosen and the Other box is not editable. */
  disabled?: boolean;
  label: string;
}

interface Opt {
  value: string;
  label: string;
  description?: string;
  other?: boolean;
}

export function OptionCards({
  type = "choice",
  options,
  allowOther,
  otherLabel,
  otherPlaceholder,
  onOtherChange,
  defaultOther,
  value,
  defaultValue,
  onChange,
  yesLabel,
  noLabel,
  disabled,
  label,
}: Readonly<OptionCardsProps>) {
  let opts: Opt[] =
    type === "yesno"
      ? [
          { value: "yes", label: nonEmpty(yesLabel) ?? "Yes" },
          { value: "no", label: nonEmpty(noLabel) ?? "No" },
        ]
      : (options ?? []);
  if (type === "choice" && truthy(allowOther))
    opts = opts.concat([{ value: OTHER_VALUE, label: nonEmpty(otherLabel) ?? "Other", other: true }]);
  const [cur, set] = useControlled<string | undefined>(value, defaultValue, (v) => v !== undefined && onChange?.(v));
  const [other, setOther] = useState(defaultOther ?? "");
  const focusOther = useRef(false); // true after a click on "Other"; keyboard selection keeps the focus on the card
  const group = useRef<HTMLDivElement>(null);
  const otherBox = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (focusOther.current && cur === OTHER_VALUE) otherBox.current?.focus();
    focusOther.current = false;
  }, [cur]);

  function onKey(e: KeyboardEvent, i: number) {
    const n = radioMove(e.key, i, opts.length);
    if (n == null) return;
    e.preventDefault();
    focusOther.current = false;
    set(opts[n].value);
    group.current?.querySelectorAll<HTMLElement>('[role="radio"]')[n]?.focus();
  }

  return (
    <div ref={group} className={cx("gdg-oc", `gdg-oc--${type}`)} role="radiogroup" aria-label={label}>
      {opts.map((op, i) => {
        const on = cur === op.value;
        return (
          <div key={op.value} className={cx("gdg-oc__card", on && "is-on")}>
            <button
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={on || (cur == null && i === 0) ? 0 : -1}
              className="gdg-oc__btn"
              disabled={disabled}
              onClick={() => {
                set(op.value);
                focusOther.current = op.other === true && cur !== OTHER_VALUE;
                if (truthy(op.other) && cur === OTHER_VALUE) otherBox.current?.focus();
              }}
              onKeyDown={(e) => {
                onKey(e, i);
              }}
            >
              {type === "yesno" ? (
                <Icon name={op.value === "yes" ? "check" : "x"} size={18} />
              ) : (
                <span className="gdg-oc__letter" aria-hidden>
                  {OPTION_LETTERS[i]}
                </span>
              )}
              <span className="gdg-oc__text">
                <span className="gdg-oc__label">{op.label}</span>
                {truthy(op.description) ? <span className="gdg-oc__desc">{op.description}</span> : null}
              </span>
            </button>
            {truthy(op.other) && on ? (
              <textarea
                ref={otherBox}
                className="gdg-oc__other"
                rows={2}
                value={other}
                placeholder={nonEmpty(otherPlaceholder) ?? "Describe your answer"}
                aria-label="Other answer"
                readOnly={disabled}
                onChange={(e) => {
                  setOther(e.target.value);
                  onOtherChange?.(e.target.value);
                }}
              />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
