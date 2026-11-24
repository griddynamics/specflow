"use client";
// FE-DS-02 (R5): port of `Switch` in bundle.js. The state is controlled by `checked`, otherwise it keeps its own.
import { useControlled } from "./use-controlled";

export interface SwitchProps {
  label?: string;
  checked?: boolean;
  defaultChecked?: boolean;
  onChange?: (checked: boolean) => void;
  disabled?: boolean;
  /** A change is being saved: announced, but the button keeps focus (a disabled one would lose it). */
  busy?: boolean;
}

export function Switch({ label, checked, defaultChecked, onChange, disabled, busy }: Readonly<SwitchProps>) {
  const [on, set] = useControlled(checked, defaultChecked ?? false, onChange);
  return (
    <label className="gdg-switch-row">
      <button
        type="button"
        role="switch"
        className="gdg-switch"
        aria-checked={on}
        disabled={disabled}
        aria-busy={busy === true ? true : undefined}
        onClick={() => {
          set(!on);
        }}
      />
      {label === undefined || label === "" ? null : <span>{label}</span>}
    </label>
  );
}
