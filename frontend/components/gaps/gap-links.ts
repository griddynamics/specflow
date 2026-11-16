// FR-GWEB-04: the ONE way to link to a gap's question card. `…/gaps?gap=<id>` opens that card on load (even when the current filters would
// hide its row). Used by the Gaps inbox and by SpecBot's decision link (a SpecBot "decision" is a gap now).
import type { RouteBuilder } from "@/lib/routes";

export function gapLink(routes: Pick<RouteBuilder, "gaps">, projectId: string, gapId: string): string {
  return routes.gaps(projectId, { gapId });
}
