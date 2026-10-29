// FE-DEC-03, FE-BOT-02.AC5 (TECH-SPECS §1.5): the ONE way to link to a decision. `…/decisions?decision=<id>` opens that decision's
// modal on load (even when the current filters would hide its row). Used by P13's SpecBot decision link. It sits on
// `routes.decisions` (lib/routes.ts is frozen), so demo and production trees both work.
import type { RouteBuilder } from "@/lib/routes";

export function decisionLink(routes: Pick<RouteBuilder, "decisions">, projectId: string, decisionId: string): string {
  return routes.decisions(projectId, { decisionId });
}
