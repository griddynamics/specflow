// The Gaps hub's dialog: the open gaps, read-only. Blockers come first. Answering is not done here (D10): people answer in Slack or Jira,
// coding agents over MCP.
import { Button, DialogFoot, LineList, Notice, Stack } from "@graphico/ui";
import { KIND_LABEL, SEVERITY_LABEL } from "@/components/gaps/gaps-logic";
import { CANVAS_COPY } from "./canvas-copy";
import type { GapRow, GapsSummary } from "./canvas-types";

/** The most lines the dialog lists; the rest is a count. */
export const GAP_LINES = 50;

export const gapsTitle = (g: GapsSummary | null): string =>
  g === null || g.critical === 0 ? CANVAS_COPY.gapsTitle : CANVAS_COPY.gapsCriticalTitle.replace("{n}", String(g.critical));

export function GapsDialog({ rows, onClose }: Readonly<{ rows: readonly GapRow[]; onClose: () => void }>) {
  const shown = rows.slice(0, GAP_LINES);
  return (
    <Stack>
      <Notice>{CANVAS_COPY.gapsIntro}</Notice>
      {rows.length === 0 ? (
        <Notice>{CANVAS_COPY.gapsNone}</Notice>
      ) : (
        <LineList
          label={CANVAS_COPY.gapsTitle}
          items={shown.map((g) => ({ key: g.id, title: g.title, detail: `${KIND_LABEL[g.kind]} · ${SEVERITY_LABEL[g.severity]}` }))}
        />
      )}
      {rows.length > shown.length ? <Notice>{CANVAS_COPY.gapsMore.replace("{n}", String(rows.length - shown.length))}</Notice> : null}
      <DialogFoot>
        <span />
        <Button variant="ghost" onClick={onClose}>
          {CANVAS_COPY.done}
        </Button>
      </DialogFoot>
    </Stack>
  );
}
