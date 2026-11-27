// The Gaps hub's dialog: how many open gaps of each category, by severity, read-only. Answering is not done here (D10): people answer in
// Slack or Jira, coding agents over MCP; the Gaps screens list them one by one.
import { Button, CountTable, DialogFoot, Notice, Stack } from "@graphico/ui";
import { plural } from "@/components/shared/sources/kb-logic";
import { KIND_LABEL, SEVERITY_LABEL } from "@/components/shared/gaps/gaps-logic";
import { gapCounts } from "./canvas-gaps";
import { CANVAS_COPY } from "./canvas-copy";
import type { GapRow, GapsSummary } from "./canvas-types";

/** The title says what the list holds: the open gaps. */
export function gapsTitle(rows: readonly GapRow[], summary: GapsSummary | null): string {
  // The API's count of the gaps (found minus settled), not the first page of rows; "N+" when the page was full.
  const open = summary === null ? rows.length : summary.total - summary.answered;
  if (open <= 0) return CANVAS_COPY.gapsTitle;
  return summary?.more === true ? `${String(open)}+ open gaps` : plural(open, "open gap");
}

export function GapsDialog({ rows, onClose }: Readonly<{ rows: readonly GapRow[]; onClose: () => void }>) {
  const counts = gapCounts(rows);
  const critical = rows.filter((g) => g.severity === "blocker").length;
  return (
    <Stack>
      <Notice>{CANVAS_COPY.gapsIntro}</Notice>
      {critical === 0 ? null : <Notice>{CANVAS_COPY.gapsCriticalLine.replace("{n}", String(critical))}</Notice>}
      {rows.length === 0 ? (
        <Notice>{CANVAS_COPY.gapsNone}</Notice>
      ) : (
        <CountTable
          label={CANVAS_COPY.gapsTitle}
          head={CANVAS_COPY.gapsCategory}
          columns={[...counts.severities.map((sv) => SEVERITY_LABEL[sv]), ...(counts.withTotal ? [CANVAS_COPY.gapsTotal] : [])]}
          rows={counts.rows.map((r) => ({ key: r.kind, label: KIND_LABEL[r.kind], counts: r.counts }))}
        />
      )}
      <DialogFoot>
        <span />
        <Button variant="ghost" onClick={onClose}>
          {CANVAS_COPY.done}
        </Button>
      </DialogFoot>
    </Stack>
  );
}
