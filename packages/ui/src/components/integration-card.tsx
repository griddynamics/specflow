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

export interface PickTool {
  label: string;
  /** Inert when there is nothing to change (everything is already checked, say). */
  disabled?: boolean;
  onClick: () => void;
}

/** A group of checkboxes with a legend (the repositories of a project). `tools` are quiet text buttons above the rows on the right (Select all,
 * Select none); `notes` are quiet lines under the rows, announced when they change; `scroll` keeps a long list inside its own box so the dialog's foot stays in view. */
export function PickList({
  legend,
  tools,
  scroll = false,
  notes,
  children,
}: Readonly<{
  legend: string;
  tools?: readonly PickTool[];
  scroll?: boolean;
  notes?: readonly string[];
  children: ReactNode;
}>): JSX.Element {
  return (
    <fieldset className="gx-pick">
      <legend className="gx-eyebrow">{legend}</legend>
      {tools === undefined ? null : (
        <div className="gx-pick__tools">
          {tools.map((tool) => (
            <button key={tool.label} type="button" className="gx-pick__tool" disabled={tool.disabled} onClick={tool.onClick}>
              {tool.label}
            </button>
          ))}
        </div>
      )}
      {scroll ? <div className="gx-pick__rows">{children}</div> : children}
      {notes === undefined ? null : (
        <div className="gx-pick__notes" aria-live="polite">
          {notes.map((note) => (
            <p key={note}>{note}</p>
          ))}
        </div>
      )}
    </fieldset>
  );
}

/** One checkbox row. By default the label is set in the mono face (a repository). `text` sets it in the body font instead; `code` is a mono lead
 * before it (an issue key) and `hint` a quiet, smaller line after it (the folder of a file). `note` sits quiet on the right. */
export function PickRow({
  label,
  code,
  hint,
  note,
  text = false,
  checked,
  disabled = false,
  onChange,
}: Readonly<{
  label: string;
  code?: string;
  hint?: string;
  note?: string;
  text?: boolean;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}>): JSX.Element {
  const plain = !text && code === undefined && hint === undefined;
  return (
    <label className={text ? "gx-pick__row gx-pick__row--text" : "gx-pick__row"}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => {
          onChange(e.target.checked);
        }}
      />
      {plain ? (
        <span>{label}</span>
      ) : (
        <span className="gx-pick__name">
          {code === undefined ? null : <code className="gx-pick__code">{code}</code>}
          {code === undefined ? "" : " "}
          {label}
          {hint === undefined ? null : (
            <>
              {" "}
              <span className="gx-pick__hint">{hint}</span>
            </>
          )}
        </span>
      )}
      {note === undefined ? null : <small>{note}</small>}
    </label>
  );
}
