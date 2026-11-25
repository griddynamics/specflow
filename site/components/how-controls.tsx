import type { JSX, ReactNode } from "react";
import { Button } from "@graphico/ui";
import { count, planSummary, type PlanState } from "../lib/plan-state";
import { NODES } from "../lib/how-graph";
import type { StageStep } from "./how-stage";

function Dot({ children }: Readonly<{ children: ReactNode }>): JSX.Element {
  return (
    <svg viewBox="0 0 14 14" aria-hidden="true">
      {children}
    </svg>
  );
}

/** What the figure shows, under it. */
export function HowLegend({ step }: Readonly<{ step: StageStep }>): JSX.Element {
  return (
    <div className="st-legend">
      {step === 1 ? <span>Sources feed sections, sections become linked items.</span> : null}
      {step === 2 ? (
        <span>
          <b className="tag solid">!</b> a problem Gap found
        </span>
      ) : null}
      {step === 3 ? (
        <>
          <span>
            <Dot>
              <circle cx="7" cy="7" r="5.5" fill="currentColor" stroke="currentColor" />
            </Dot>
            done
          </span>
          <span>
            <Dot>
              <circle cx="7" cy="7" r="5.5" fill="url(#hatch)" stroke="currentColor" strokeWidth="2" />
            </Dot>
            in progress
          </span>
          <span>
            <Dot>
              <circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" strokeWidth="2.6" />
            </Dot>
            ready
          </span>
          <span>
            <Dot>
              <circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" strokeDasharray="2 2" />
            </Dot>
            waiting or blocked
          </span>
        </>
      ) : null}
    </div>
  );
}

export interface PlanActions {
  onRound: () => void;
  onAnswer: () => void;
  onReset: () => void;
}

/** Step 3 is a small working state machine; steps 1 and 2 only say what the figure shows. */
export function HowControls({ step, plan, actions }: Readonly<{ step: StageStep; plan: PlanState; actions: PlanActions }>): JSX.Element {
  if (step === 1) {
    return (
      <div className="stage-ctl">
        <span className="status">Every item keeps a link to the sentence it came from.</span>
      </div>
    );
  }
  if (step === 2) {
    return (
      <div className="stage-ctl">
        <span className="status">3 problems found: a missing piece, a contradiction and vague wording.</span>
      </div>
    );
  }
  const allDone = count(plan, "done") === NODES.length;
  return (
    <div className="stage-ctl">
      <Button size="sm" disabled={allDone} onClick={actions.onRound}>
        Next round
      </Button>
      <Button variant="ghost" size="sm" disabled={plan.answered} onClick={actions.onAnswer}>
        Answer the open question
      </Button>
      <button className="linkbtn" type="button" onClick={actions.onReset}>
        Reset
      </button>
      <span className="status" aria-live="polite">
        {planSummary(plan)}
      </span>
    </div>
  );
}
