// FE-DEC-01, FE-DS-02 (R5), PQ9 (owner Q24, Q25, G-SEV-1): the quiet severity badge. The word always
// shows (never colour alone), in ink, with no background and no bars; only Critical carries an
// exclamation icon before the word. Task 4: the Graphico palette is monochrome, so the icon marks
// Critical by its shape while the word carries the level; styling: `shared-ui.css`.
import { Icon } from "./Icon";
import { SEVERITY } from "./app-logic";
import { nonEmpty } from "./utils";
import "./shared-ui.css";

export type SeverityLevel = "critical" | "high" | "medium" | "low";

export interface SeverityBadgeProps {
  level: SeverityLevel;
  label?: string;
}

// Looked up by any string: a level outside the set reads as "low".
const SEVERITY_BY_LEVEL: Partial<Record<string, (typeof SEVERITY)[SeverityLevel]>> = SEVERITY;

export function SeverityBadge({ level, label }: Readonly<SeverityBadgeProps>) {
  const lv = SEVERITY_BY_LEVEL[level] ?? SEVERITY.low;
  return (
    <span className="sui-sev" data-testid="severity-badge">
      {level === "critical" && <Icon name="warning-circle" size={16} className="sui-sev__icon" />}
      {nonEmpty(label) ?? lv.word}
    </span>
  );
}
