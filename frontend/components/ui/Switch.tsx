"use client";
// FE-DS-02, FE-DOD-05 (R5): port of `Switch` in bundle.js. A native button with role=switch: Space and Enter toggle it.
import { useControlled } from "./use-controlled";
import { truthy } from "./utils";

export interface SwitchProps {
  label?: string;
  checked?: boolean;
  defaultChecked?: boolean;
  onChange?: (checked: boolean) => void;
  disabled?: boolean;
}

export function Switch({ label, checked, defaultChecked, onChange, disabled }: Readonly<SwitchProps>) {
  const [on, set] = useControlled(checked, defaultChecked === true, onChange);
  return (
    <label className="gdg-switch-row">
      <button
        type="button"
        role="switch"
        className="gdg-switch"
        aria-checked={on}
        disabled={disabled}
        onClick={() => {
          set(!on);
        }}
      />
      {truthy(label) ? <span>{label}</span> : null}
    </label>
  );
}
