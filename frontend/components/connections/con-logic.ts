// FE-CON-01.AC2..AC5 (PLAN a.10): pure filter logic of the Connections screen (node-testable).
import type { Connector } from "@/lib/domain/connectors";
import { matchesQuery } from "@/components/ui/SearchField";

export const ALL_CATEGORY = "All";

/** Category (exact; "All" = every connector) and search (case-insensitive match in the name or the description), both must hold. */
export function filterConnectors(items: Connector[], category: string, query: string): Connector[] {
  const q = query.trim();
  return items.filter(
    (c) =>
      (category === ALL_CATEGORY || c.category === category) && (q === "" || matchesQuery(c.name, q) || matchesQuery(c.description, q)),
  );
}

/** MOCKS §9: Connected -> Manage, Needs attention -> Reconnect, Not connected -> Connect. */
export function actionLabel(status: Connector["status"]): string {
  if (status === "connected") return "Manage";
  return status === "attention" ? "Reconnect" : "Connect";
}
