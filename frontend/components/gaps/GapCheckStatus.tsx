"use client";
// The gap check status line of the Gaps screen (docs/graph/gap/lifecycle.md "Plan link": build, then gap run, then plan run; api.md
// "Host runner"). After a knowledge build Specflow looks for gaps on its own; this line says so: "Waiting for the knowledge build" while the
// build runs, "Looking for gaps" with the step and a progress bar while the gap run runs, then when the last check finished and what it
// found. `useGapCheck` reads `GapsPort.check` every 3 s while something runs and every 30 s otherwise, and calls `onFinished` when a run
// it saw running has ended, so the lists read again; the screen also uses the check to keep the inbox live and to mark New gaps. A failed
// read keeps the last line (or shows nothing before the first one). While the run runs the line also counts the New rows the inbox lists.
import { useEffect, useRef, useState } from "react";
import { ProgressBar, Spinner } from "@/components/ui/ProgressBar";
import type { GapCheck, GapsPort } from "@/lib/domain/gaps";
import { checkActive, checkProgress, checkText, newListedText } from "@/components/shared/gaps/gaps-logic";

const ACTIVE_MS = 3_000;
const IDLE_MS = 30_000;

/** The latest gap check, read again while something runs; null until the first read answers. */
export function useGapCheck(gaps: GapsPort, projectId: string, onFinished: () => void): GapCheck | null {
  const [check, setCheck] = useState<GapCheck | null>(null);
  const finished = useRef(onFinished);
  useEffect(() => {
    finished.current = onFinished;
  }, [onFinished]);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let wasActive = false;
    const read = () => {
      gaps.check(projectId).then(
        (c) => {
          if (!alive) return;
          setCheck(c);
          const active = checkActive(c);
          if (wasActive && !active) finished.current();
          wasActive = active;
          timer = setTimeout(read, active ? ACTIVE_MS : IDLE_MS);
        },
        () => {
          if (alive) timer = setTimeout(read, wasActive ? ACTIVE_MS : IDLE_MS);
        },
      );
    };
    read();
    return () => {
      alive = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [gaps, projectId]);
  return check;
}

/** The status line; `listed` is the number of New rows the inbox lists (shown while the run runs, from one). */
export function GapCheckStatus({ check, listed = 0 }: Readonly<{ check: GapCheck | null; listed?: number }>) {
  if (check === null) return null;
  const { title, detail } = checkText(check);
  const active = checkActive(check);
  return (
    <section className="gap-check" aria-label="Gap check" aria-live="polite" data-testid="gap-check" data-phase={check.phase}>
      <div className="gap-check__head">
        {active ? <Spinner size={16} label={title} /> : null}
        <p className="gap-check__title body-sm" data-testid="gap-check-title">
          {title}
        </p>
      </div>
      {detail === "" ? null : (
        <p className="gap-check__detail body-sm" data-testid="gap-check-detail">
          {detail}
        </p>
      )}
      {check.phase === "running" && listed > 0 ? (
        <p className="gap-check__detail body-sm" data-testid="gap-check-listed">
          {newListedText(listed)}
        </p>
      ) : null}
      {check.phase === "running" ? <ProgressBar value={checkProgress(check)} label="Gap check progress" /> : null}
    </section>
  );
}
