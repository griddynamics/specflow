// FE-DEC-01, FE-DS-02 (R5), PQ9 (owner Q24, Q25, G-SEV-1): the quiet severity badge. The word always shows (never colour alone), in `--ink`, with no
// background and no bars; only Critical carries a red exclamation icon before the word. Styling: `ui-gaps-app.css`.
import { Icon } from "./Icon";
import { SEVERITY } from "./app-logic";
import { cx, nonEmpty } from "./utils";

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
    <span className={cx("gdg-sev", `gdg-sev--${nonEmpty(level) ?? "low"}`)} data-testid="severity-badge">
      {level === "critical" && <Icon name="warning-circle" size={16} className="gdg-sev__icon" />}
      {nonEmpty(label) ?? lv.word}
    </span>
  );
}
