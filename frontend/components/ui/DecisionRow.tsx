// FE-DEC-01, FE-DS-02 (R5): port of `DecisionRow` in bundle.js. One row, a single button: title and subtitle, domain Tag,
// SeverityBadge, assignee avatar and name ("Unassigned" when none), caret. FR-GWEB-04: the Gaps inbox passes `who` (the gap's origin) for the
// last cell and `severityLabel` (Blocker, Major, Minor); without them the row renders as before.
import type { ReactNode } from "react";
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
}: Readonly<DecisionRowProps>) {
  return (
    <li className={cx("gdg-drow", status === "answered" && "is-answered")}>
      <button type="button" className="gdg-drow__btn" onClick={onClick}>
        <span className="gdg-drow__title">
          <b>{title}</b>
          {truthy(subtitle) ? <small>{subtitle}</small> : null}
        </span>
        <span className="gdg-drow__cell">
          <Tag>{domain}</Tag>
        </span>
        <span className="gdg-drow__cell">
          <SeverityBadge level={severity} label={severityLabel} />
        </span>
        <span className="gdg-drow__who">
          {who ??
            (truthy(assignee) ? (
              <>
                <Avatar name={assignee} size="sm" title={false} />
                <span>{assignee}</span>
              </>
            ) : (
              <span className="gdg-muted">Unassigned</span>
            ))}
        </span>
        <Icon name="caret-right" size={16} className="gdg-drow__go" />
      </button>
    </li>
  );
}
