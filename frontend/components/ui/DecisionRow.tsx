// FE-DEC-01, FE-DS-02 (R5): port of `DecisionRow` in bundle.js. One row, a single button: title and subtitle, domain Tag,
// SeverityBadge, assignee avatar and name ("Unassigned" when none), caret. FR-GWEB-04, FR-GWEB-06: the Gaps inbox passes the gap's
// `assignee` and `severityLabel` (Blocker, Major, Minor); `who` replaces the last cell where a screen needs other content. `aside` is a
// control over the assignee cell (the Gaps inbox's assignee menu): a button cannot hold another control, so it sits beside the row's
// button, laid over that cell (ui-gaps.css), and a click on it never opens the row. `badge` is a Tag beside the title (the Gaps inbox's
// "Resolving…" while a gap's rebuild runs).
import type { ReactNode } from "react";
import "./ui-gaps.css";
import { Avatar } from "./Avatar";
import { Icon } from "./Icon";
import { SeverityBadge, type SeverityBadgeProps } from "./SeverityBadge";
import { Tag } from "./Tag";
import { cx, truthy } from "./utils";

export interface DecisionRowProps {
  title: string;
  subtitle?: string;
  domain: string;
  severity: SeverityBadgeProps["level"];
  assignee?: string;
  status?: "open" | "answered";
  onClick?: () => void;
  /** Replaces the assignee cell (avatar and name). */
  who?: ReactNode;
  /** The severity word when it is not the level's own (SeverityBadge `label`). */
  severityLabel?: string;
  /** A control in the assignee cell, outside the row's button; the cell under it stays empty. */
  aside?: ReactNode;
  /** A Tag on the title's line, after the title. */
  badge?: ReactNode;
}

/** The default assignee cell: the avatar and name, or "Unassigned". */
function Assignee({ name }: Readonly<{ name?: string }>) {
  if (!truthy(name)) return <span className="gdg-muted">Unassigned</span>;
  return (
    <>
      <Avatar name={name} size="sm" title={false} />
      <span>{name}</span>
    </>
  );
}

export function DecisionRow({
  title,
  subtitle,
  domain,
  severity,
  assignee,
  status,
  onClick,
  who,
  severityLabel,
  aside,
  badge,
}: Readonly<DecisionRowProps>) {
  const control = aside !== undefined && aside !== null;
  return (
    <li className={cx("gdg-drow", status === "answered" && "is-answered", control && "gdg-drow--aside")}>
      <button type="button" className="gdg-drow__btn" onClick={onClick}>
        <span className="gdg-drow__title">
          {truthy(badge) ? (
            <span className="gdg-drow__line">
              <b>{title}</b>
              {badge}
            </span>
          ) : (
            <b>{title}</b>
          )}
          {truthy(subtitle) ? <small>{subtitle}</small> : null}
        </span>
        <span className="gdg-drow__cell">
          <Tag>{domain}</Tag>
        </span>
        <span className="gdg-drow__cell">
          <SeverityBadge level={severity} label={severityLabel} />
        </span>
        <span className="gdg-drow__who" aria-hidden={control || undefined}>
          {control ? null : (who ?? <Assignee name={assignee} />)}
        </span>
        <Icon name="caret-right" size={16} className="gdg-drow__go" />
      </button>
      {control ? <div className="gdg-drow__aside">{aside}</div> : null}
    </li>
  );
}
