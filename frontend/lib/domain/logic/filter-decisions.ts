// FE-DEC-01 (TECH-SPECS §2.10). Signature final (P02); body by P12.
import type { Decision, DecisionFilters } from "../decisions";

/**
 * All four filters combine with AND (FE-DEC-01.AC6). Pure: keeps the input order, never mutates `list`, returns a new array
 * (the same Decision objects). `domain: "all"` and `severity: "all"` match everything; `meId` is the active user from the
 * people port, never a literal name.
 */
export function filterDecisions(list: Decision[], f: DecisionFilters, meId: string): Decision[] {
  return list.filter(
    (d) =>
      (!f.openOnly || d.status === "open") &&
      (!f.assignedToMe || d.assigneeId === meId) &&
      (f.domain === "all" || d.domain === f.domain) &&
      (f.severity === "all" || d.severity === f.severity),
  );
}
