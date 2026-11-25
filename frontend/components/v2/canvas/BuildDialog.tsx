"use client";
// Build or Update the graph: the one decision of the Graph hub. The reasoning level is 1 (Low) to 5 (Max) with the V1 estimate; the build
// starts with `knowledge.build` and its failure, if any, shows on the status, not here (V1 `useBuild`).
import { useState } from "react";
import { Button, DialogFoot, Notice, Stack } from "@graphico/ui";
import { estimateText, plural, REASONING_LEVELS } from "@/components/mission-control/sources/kb-logic";
import { useBuild } from "@/components/mission-control/sources/use-sources-actions";
import { isReasoningLevel, type ProjectSourcesStatus, type ReasoningLevel } from "@/lib/domain/sources";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";

/** V1's default level: Medium. */
const DEFAULT_LEVEL: ReasoningLevel = 2;

export function BuildDialog({
  projectId,
  status,
  update,
  onStarted,
  onClose,
}: Readonly<{ projectId: string; status: ProjectSourcesStatus; update: boolean; onStarted: () => void; onClose: () => void }>) {
  const { knowledge } = useV2Ports();
  const build = useBuild(knowledge, projectId);
  const [level, setLevel] = useState<ReasoningLevel>(DEFAULT_LEVEL);
  const docs = status.totals.documents;
  // The nodes count files, so the text does: the files that are done.
  const files = status.sources.reduce((n, s) => n + s.counts.done, 0);
  const intro = (update ? CANVAS_COPY.updateIntro : CANVAS_COPY.buildIntro)
    .replace("{docs}", plural(files, "file"))
    .replace("{sources}", plural(status.sources.length, "source"));
  return (
    <Stack>
      <Notice>{intro}</Notice>
      {status.buildFailure === null ? null : (
        <Notice role="alert">{CANVAS_COPY.buildFailed.replace("{code}", status.buildFailure.code)}</Notice>
      )}
      <div role="group" aria-label={CANVAS_COPY.levels} className="gx-row">
        {REASONING_LEVELS.map((name, i) => {
          const value = i + 1;
          return (
            <Button
              key={name}
              size="sm"
              variant={value === level ? "ink" : "ghost"}
              aria-pressed={value === level}
              onClick={() => {
                if (isReasoningLevel(value)) setLevel(value);
              }}
            >
              {name}
            </Button>
          );
        })}
      </div>
      <Notice>{CANVAS_COPY.estimate.replace("{time}", estimateText(docs, level))}</Notice>
      <DialogFoot>
        <Button variant="ghost" onClick={onClose}>
          {CANVAS_COPY.close}
        </Button>
        <Button
          variant="yellow"
          onClick={() => {
            onStarted();
            void build(level);
            onClose();
          }}
        >
          {update ? CANVAS_COPY.update : CANVAS_COPY.build}
        </Button>
      </DialogFoot>
    </Stack>
  );
}
