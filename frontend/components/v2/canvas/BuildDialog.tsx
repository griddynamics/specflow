"use client";
// Build or Update the graph: the one decision of the Graph hub. The reasoning level is 1 (Low) to 5 (Max) with the V1 estimate; the build
// starts with `knowledge.build`. The dialog waits for the answer: the hub is marked as starting only when the start was accepted, and a start the
// API refuses (a build already running, the service down) shows here, as in the plan dialog, and the dialog stays open.
import { useState } from "react";
import { Button, DialogFoot, Notice, Stack } from "@graphico/ui";
import { estimateText, plural, REASONING_LEVELS } from "@/components/shared/sources/kb-logic";
import { isDataError } from "@/lib/domain/errors";
import { isReasoningLevel, type ProjectSourcesStatus, type ReasoningLevel } from "@/lib/domain/sources";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";

/** V1's default level: Medium. */
const DEFAULT_LEVEL: ReasoningLevel = 2;

/** A start the API refuses because a build is running already (409) is not a failure to retry. */
export const buildStartFailure = (e: unknown): string =>
  isDataError(e) && e.code === "conflict" ? CANVAS_COPY.buildRunning : CANVAS_COPY.buildStartFailed;

export function BuildDialog({
  projectId,
  status,
  update,
  onStarted,
  onClose,
}: Readonly<{ projectId: string; status: ProjectSourcesStatus; update: boolean; onStarted: () => void; onClose: () => void }>) {
  const { knowledge } = useV2Ports();
  const [level, setLevel] = useState<ReasoningLevel>(DEFAULT_LEVEL);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const docs = status.totals.documents;
  // The nodes count files, so the text does: the files that are done.
  const files = status.sources.reduce((n, s) => n + s.counts.done, 0);
  const intro = (update ? CANVAS_COPY.updateIntro : CANVAS_COPY.buildIntro)
    .replace("{docs}", plural(files, "file"))
    .replace("{sources}", plural(status.sources.length, "source"));
  const start = () => {
    setBusy(true);
    setFailure(null);
    knowledge.build(projectId, { reasoningLevel: level }).then(
      () => {
        onStarted();
        onClose();
      },
      (e: unknown) => {
        setBusy(false);
        setFailure(buildStartFailure(e));
      },
    );
  };
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
      {failure === null ? null : <Notice role="alert">{failure}</Notice>}
      <DialogFoot>
        <Button variant="ghost" onClick={onClose}>
          {CANVAS_COPY.close}
        </Button>
        <Button variant="yellow" disabled={busy} onClick={start}>
          {update ? CANVAS_COPY.update : CANVAS_COPY.build}
        </Button>
      </DialogFoot>
    </Stack>
  );
}
