"use client";
// FE-DS-02, FE-DOD-05.AC5 (R5): port of `Select` in bundle.js. It is a Menu with a pill trigger, so it shares the Menu keyboard model.
import { Icon, type IconName } from "./Icon";
import { Menu } from "./Menu";
import { cx, nonEmpty, truthy } from "./utils";
import { useControlled } from "./use-controlled";

export interface SelectProps {
  options: { value: string; label: string; description?: string }[];
  value?: string;
  defaultValue?: string;
  onChange?: (v: string) => void;
  label?: string;
  icon?: IconName;
  placeholder?: string;
  /** Starts with the list open (previews only). */
  defaultOpen?: boolean;
  className?: string;
}

export function Select({
  options,
  value,
  defaultValue,
  onChange,
  label,
  icon,
  placeholder,
  defaultOpen,
  className,
}: Readonly<SelectProps>) {
  const [cur, set] = useControlled<string | undefined>(
    value,
    defaultValue ?? options[0]?.value,
    onChange as ((v: string | undefined) => void) | undefined,
  );
  const curOpt = options.find((o) => o.value === cur);
  return (
    <div className={cx("gdg-select", className)} data-testid={truthy(label) ? `select-${label}` : undefined}>
      {truthy(label) ? <span className="gdg-select__label">{label}</span> : null}
      <Menu
        label={nonEmpty(label) ?? "Options"}
        defaultOpen={defaultOpen}
        triggerClassName="gdg-select__trigger"
        trigger={
          <>
            {icon ? <Icon name={icon} size={16} /> : null}
            <span className="gdg-select__value">{curOpt ? curOpt.label : (nonEmpty(placeholder) ?? "Select")}</span>
          </>
        }
        items={options.map((o) => ({
          label: o.label,
          description: o.description,
          checked: o.value === cur,
          onSelect: () => {
            set(o.value);
          },
        }))}
      />
    </div>
  );
}
