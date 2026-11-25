"use client";
// Build the plan or Replan: the second decision, on the Plan hub. Replan is today's replan (`plan.create({ replan: true })`, D9): it plans every
// area again; refining instead is later. The open gaps are said, not asked about: the plan is yellow as soon as the gaps are found, answered or not.
import { useState } from "react";
import { Button, DialogFoot, Notice, Stack } from "@graphico/ui";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import type { GapsSummary } from "./canvas-types";

export function PlanDialog({
  projectId,
  replan,
  gaps,
  destination,
  onClose,
}: Readonly<{ projectId: string; replan: boolean; gaps: GapsSummary | null; destination: string | null; onClose: () => void }>) {
  const { plan } = useV2Ports();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const open = gaps === null ? 0 : gaps.total - gaps.answered;
  const critical = gaps === null || gaps.critical === 0 ? "" : CANVAS_COPY.gapsCritical.replace("{n}", String(gaps.critical));
  const start = () => {
    setBusy(true);
    setFailed(false);
    plan.create(projectId, { replan }).then(onClose, () => {
      setBusy(false);
      setFailed(true);
    });
  };
  return (
    <Stack>
      <Notice>{replan ? CANVAS_COPY.replanIntro : CANVAS_COPY.planIntro}</Notice>
      {destination === null ? null : <Notice>{CANVAS_COPY.planTo.replace("{name}", destination)}</Notice>}
      {open > 0 ? <Notice>{CANVAS_COPY.gapsOpen.replace("{open}", String(open)).replace("{critical}", critical)}</Notice> : null}
      {failed ? <Notice role="alert">{CANVAS_COPY.planFailed}</Notice> : null}
      <DialogFoot>
        <Button variant="ghost" onClick={onClose}>
          {CANVAS_COPY.close}
        </Button>
        <Button variant="yellow" disabled={busy} onClick={start}>
          {replan ? CANVAS_COPY.replanStart : CANVAS_COPY.planStart}
        </Button>
      </DialogFoot>
    </Stack>
  );
}
