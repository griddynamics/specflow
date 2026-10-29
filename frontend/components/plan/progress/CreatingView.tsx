"use client";
// FE-PLAN-04 (TECH-SPECS §2.15; DECISIONS P19-12, P23-*): Stage 1. One strong glass card "Creating your plan" on the soft `.gdg-scene`
// (as Mission Control Processing), a ToolSteps list, a ProgressBar and a polite live region. No Stage 0 element and no primary button.
//  - Progress comes from the port: `run` (step, total, label) changes with each `plan.step` event; there is no timer here. The labels are
//    remembered as they arrive (the first four go from running to done, the last runs until the plan is ready).
//  - The URL is cleaned with `replaceState` when Stage 1 starts (R23-01) and again when the plan is ready, so Stage 2 never renders a stale
//    `?view=` and opens on Plan (FE-PLAN-05.AC1). Leaving unsubscribes, so a plan that is ready later cannot touch another screen's URL.
//  - One polite live region (R23-03): the card holds one visually hidden block with a line per label received, in the order of the events
//    (R23-09: a batched render cannot skip a label, because the lines come from the remembered event labels, not from `run.label`); the visual list and bar are
//    `aria-hidden`, so a screen reader does not also hear the spinner's "Loading" or the percent.
import { useEffect, useRef, useState } from "react";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { ToolSteps } from "@/components/ui/ToolSteps";
import { focusIsLost } from "@/components/ui/use-outside";
import { usePlanPort, usePlanRoutes } from "../use-plan-ports";
import { forgetOpen } from "../tasks/open-state";
import type { PlanState } from "@/lib/domain/plan";
import { announceLines, progressValue, rememberLabel, stepRows, type KnownLabels } from "./creating-steps";
import "./progress.css";

export function CreatingView({ projectId, run }: Readonly<{ projectId: string; run: Extract<PlanState, { phase: "creating" }> }>) {
  const plan = usePlanPort();
  const routes = usePlanRoutes();
  const [known, setKnown] = useState<KnownLabels>(() => rememberLabel({}, run.step, run.label));
  const title = useRef<HTMLHeadingElement>(null);

  // The label of each new step (stored from the previous render's props: the usual "derive state while rendering" pattern).
  const next = rememberLabel(known, run.step, run.label);
  if (next !== known) setKnown(next);

  // The Create PLAN button that had the focus is gone: focus the card title, unless the user already moved on to something else.
  useEffect(() => {
    if (focusIsLost()) title.current?.focus();
  }, []);

  // P26 (P24 m4): a new creation run starts with nothing remembered about open phases (the first phase opens again).
  useEffect(() => {
    forgetOpen(projectId);
  }, [projectId]);

  // R23-01: a `?view=` left in the URL (Stage 0 was opened with it) must be gone before Stage 2 can render.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).has("view")) window.history.replaceState(null, "", routes.plan(projectId));
  }, [projectId, routes]);

  // Each event brings its own label: React may batch two events into one render (a long task), so the prop alone could skip a label.
  // Stage 1 -> 2 uses a clean URL (a `?view=develop` opened before the plan existed must not apply, as Mission Control README).
  useEffect(() => {
    return plan.subscribe(projectId, (e, state) => {
      if (state.phase === "creating") setKnown((k) => rememberLabel(k, state.step, state.label));
      if (e.type === "plan.ready") window.history.replaceState(null, "", routes.plan(projectId));
    });
  }, [plan, projectId, routes]);

  const steps = stepRows(next, run.step);
  return (
    <div className="plan-scene gdg-scene" data-testid="plan-creating" data-project={projectId} data-step={run.step}>
      <GlassSurface variant="strong" className="plan-card" aria-live="polite" aria-atomic="false" data-testid="plan-card">
        <h2 className="title-3 plan-card__title" tabIndex={-1} ref={title}>
          Creating your plan
        </h2>
        <div className="plan-sr" data-testid="plan-announce">
          {announceLines(next).map((l) => (
            <p key={l.step} data-testid="plan-announce-line">
              {l.label}
            </p>
          ))}
        </div>
        <div className="plan-card__visual" aria-hidden="true">
          <ToolSteps steps={steps} />
          <ProgressBar value={progressValue(run.step, run.total)} label={`Step ${String(run.step)} of ${String(run.total)}`} />
        </div>
      </GlassSurface>
    </div>
  );
}
