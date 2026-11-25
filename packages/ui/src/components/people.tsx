import type { JSX, ReactNode } from "react";
import { cx } from "../cx";
import { Tag } from "./tag";

const initialsOf = (name: string): string =>
  name
    .split(/[\s.@]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => (w[0] ?? "").toUpperCase())
    .join("");

/** A round badge with the initials of a name; `invited` is the dashed one for a person who has not signed in yet. */
export function Avatar({ name, invited = false }: Readonly<{ name: string; invited?: boolean }>): JSX.Element {
  return (
    <span className={cx("gx-avatar", invited && "gx-avatar--invited")} aria-hidden="true">
      {invited ? "@" : initialsOf(name)}
    </span>
  );
}

/** A bordered list of people. */
export function PersonList({ label, children }: Readonly<{ label: string; children: ReactNode }>): JSX.Element {
  return (
    <ul className="gx-people" aria-label={label}>
      {children}
    </ul>
  );
}

export interface PersonRowProps {
  name: string;
  /** The second line: the email, or what is missing. */
  detail?: string;
  invited?: boolean;
  /** Appended to the name, for the caller's own row. */
  youLabel?: string;
  tag?: string;
  /** The row's one action (a cross); its label names the row. */
  action?: { label: string; onClick: () => void };
}

export function PersonRow({ name, detail, invited = false, youLabel, tag, action }: Readonly<PersonRowProps>): JSX.Element {
  return (
    <li className="gx-person">
      <Avatar name={name} invited={invited} />
      <span className="gx-person__who">
        <b>{youLabel === undefined ? name : `${name} (${youLabel})`}</b>
        {detail === undefined || detail === "" ? null : <small>{detail}</small>}
      </span>
      {tag === undefined ? <span /> : <Tag>{tag}</Tag>}
      {action === undefined ? (
        <span />
      ) : (
        <button type="button" className="gx-person__x" aria-label={action.label} title={action.label} onClick={action.onClick}>
          ×
        </button>
      )}
    </li>
  );
}

export interface SuggestionItem {
  key: string;
  name: string;
  email: string;
  /** The suggestion the keyboard has highlighted. */
  active?: boolean;
  onPick: () => void;
}

/** The people offered under an email field. */
export function SuggestionList({ label, items }: Readonly<{ label: string; items: readonly SuggestionItem[] }>): JSX.Element {
  return (
    <ul className="gx-suggest" aria-label={label}>
      {items.map((item) => (
        <li key={item.key}>
          <button type="button" className="gx-suggest__item" data-active={item.active === true ? true : undefined} onClick={item.onPick}>
            <Avatar name={item.name} />
            <span className="gx-person__who">
              <b>{item.name}</b>
              <small>{item.email}</small>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
