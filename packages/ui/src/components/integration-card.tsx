import type { JSX, ReactNode } from "react";
import { Button } from "./button";
import { Tag } from "./tag";
import { VendorMark } from "./vendor-mark";
import type { VendorKind } from "./vendor-mark";

/** `off`: not connected (grey, dashed); `on`: connected and working (ink); `stopped`: connected, but nothing acts (ink outline, no shadow); `unavailable`: the server does not run the app. */
export type IntegrationState = "off" | "on" | "stopped" | "unavailable";

export interface IntegrationCardProps {
  marks: readonly VendorKind[];
  name: string;
  /** What the app holds. */
  sub: string;
  state: IntegrationState;
  /** The state in a word, from the API (a tag at the top right). */
  badge?: string;
  /** "Connected by <name>". */
  by?: string;
  actionLabel: string;
  onAction: () => void;
}

/** One app of the Integrations page. It only connects: no switches, no list of uses. */
export function IntegrationCard({
  marks,
  name,
  sub,
  state,
  badge,
  by,
  actionLabel,
  onAction,
}: Readonly<IntegrationCardProps>): JSX.Element {
  return (
    <li className="gx-igcard" data-state={state}>
      <div className="gx-igcard__top">
        <span className="gx-igcard__logos">
          {marks.map((kind) => (
            <span className="gx-igcard__logo" key={kind}>
              <VendorMark kind={kind} />
            </span>
          ))}
        </span>
        {badge === undefined ? null : <Tag solid={state === "on"}>{badge}</Tag>}
      </div>
      <div>
        <h2 className="gx-igcard__name">{name}</h2>
        <p className="gx-igcard__sub">{sub}</p>
      </div>
      <div className="gx-igcard__foot">
        <Button size="sm" variant={state === "on" ? "ghost" : "ink"} onClick={onAction} disabled={state === "unavailable"}>
          {actionLabel}
        </Button>
        {by === undefined ? null : <span className="gx-igcard__by">{by}</span>}
      </div>
    </li>
  );
}

/** The cards, three to a row. */
export function IntegrationGrid({ label, children }: Readonly<{ label: string; children: ReactNode }>): JSX.Element {
  return (
    <ul className="gx-iggrid" aria-label={label}>
      {children}
    </ul>
  );
}

export interface BuiltInItem {
  key: string;
  mark: VendorKind;
  title: string;
  note: string;
}

/** What is always on and needs no connecting, under the cards. */
export function BuiltInList({ heading, items }: Readonly<{ heading: string; items: readonly BuiltInItem[] }>): JSX.Element {
  return (
    <section className="gx-builtin" aria-label={heading}>
      <p className="gx-eyebrow">{heading}</p>
      <ul className="gx-builtin__list">
        {items.map((item) => (
          <li key={item.key}>
            <span className="gx-builtin__icon">
              <VendorMark kind={item.mark} />
            </span>
            <b>{item.title}</b>
            <small>{item.note}</small>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** A group of checkboxes with a legend (the repositories of a project). */
export function PickList({ legend, children }: Readonly<{ legend: string; children: ReactNode }>): JSX.Element {
  return (
    <fieldset className="gx-pick">
      <legend className="gx-eyebrow">{legend}</legend>
      {children}
    </fieldset>
  );
}

export function PickRow({
  label,
  note,
  checked,
  disabled = false,
  onChange,
}: Readonly<{ label: string; note?: string; checked: boolean; disabled?: boolean; onChange: (checked: boolean) => void }>): JSX.Element {
  return (
    <label className="gx-pick__row">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => {
          onChange(e.target.checked);
        }}
      />
      <span>{label}</span>
      {note === undefined ? null : <small>{note}</small>}
    </label>
  );
}
