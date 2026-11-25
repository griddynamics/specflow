// The Gaps hub's dialog: the open gaps, read-only. Blockers come first. Answering is not done here (D10): people answer in Slack or Jira,
// coding agents over MCP.
import { Button, DialogFoot, LineList, Notice, Stack } from "@graphico/ui";
import { plural } from "@/components/mission-control/sources/kb-logic";
import { KIND_LABEL, SEVERITY_LABEL } from "@/components/gaps/gaps-logic";
import { CANVAS_COPY } from "./canvas-copy";
import type { GapRow } from "./canvas-types";

/** The most lines the dialog lists; the rest is a count. */
export const GAP_LINES = 50;

/** The title says what the list holds: the open gaps. */
export const gapsTitle = (rows: readonly GapRow[]): string => (rows.length === 0 ? CANVAS_COPY.gapsTitle : plural(rows.length, "open gap"));

export function GapsDialog({ rows, onClose }: Readonly<{ rows: readonly GapRow[]; onClose: () => void }>) {
  const shown = rows.slice(0, GAP_LINES);
  const critical = rows.filter((g) => g.severity === "blocker").length;
  return (
    <Stack>
      <Notice>{CANVAS_COPY.gapsIntro}</Notice>
      {critical === 0 ? null : <Notice>{CANVAS_COPY.gapsCriticalLine.replace("{n}", String(critical))}</Notice>}
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
