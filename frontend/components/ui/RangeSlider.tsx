"use client";
// FE-DS-02 (R5): a labelled range with named stops (the knowledge build's reasoning level). A native `<input type="range">`: the arrow
// keys, Home, End and PageUp/PageDown work as in the browser. The label is bound like `TextField` (`useId` + `htmlFor`); the value
// is read with `valueAsNumber`. `aria-valuetext` is the stop's name and `hint` is the slider's own accessible description.
import { useId, type ReactNode } from "react";
import "./ui-gaps.css";

export interface RangeSliderProps {
  label: string;
  /** The stop names, in order; the value is the 1-based index. */
  options: readonly string[];
  value: number;
  onChange: (value: number) => void;
  hint?: ReactNode;
  testId?: string;
}

export function RangeSlider({ label, options, value, onChange, hint, testId }: Readonly<RangeSliderProps>) {
  const id = useId();
  const hid = `${id}-h`;
  return (
    <div className="gdg-range">
      <label className="gdg-field__label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className="gdg-range__input"
        type="range"
        min={1}
        max={options.length}
        step={1}
        value={value}
        aria-valuetext={options[value - 1]}
        aria-describedby={hid}
        onChange={(e) => {
          onChange(e.currentTarget.valueAsNumber);
        }}
        data-testid={testId}
      />
      <div className="gdg-range__ticks caption" aria-hidden>
        {options.map((name) => (
          <span key={name}>{name}</span>
        ))}
      </div>
      <span className="gdg-field__help" id={hid}>
        {hint}
      </span>
    </div>
  );
}
