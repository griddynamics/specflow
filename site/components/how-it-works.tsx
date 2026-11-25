"use client";

import { useEffect, useState, type JSX } from "react";
import { Button } from "@graphico/ui";
import { METHODS } from "../lib/how-graph";
import { prefersReducedMotion } from "../lib/canvas";
import { answerQuestion, initialPlan, nextRound, type PlanState } from "../lib/plan-state";
import { HowControls, HowLegend } from "./how-controls";
import { HowStage, type StageStep } from "./how-stage";

const CHIP_MS = 1100;

interface StepCopy {
  step: StageStep;
  kind: string;
  short: string;
  title: string;
  body: string[];
  chips?: string[];
  hint?: string;
}

const STEPS: StepCopy[] = [
  {
    step: 1,
    kind: "Graph",
    short: "Parse it all",
    title: "Graph turns your documents into one knowledge graph.",
    body: [
      "Connect Confluence, Jira and Google Drive, or upload files. Graph splits every document into sections and pulls out the modules, features, interfaces, rules and decisions in them. A shared ontology gives each one a type and each link a meaning, so the same thing gets one name even when ten documents call it ten ways. Every item keeps a link to the sentence it came from.",
    ],
    chips: ["Confluence", "Jira", "Google Drive", "Upload: PDF, Word, PowerPoint, Excel"],
  },
  {
    step: 2,
    kind: "Gap",
    short: "Question it",
    title: "Gap finds what is missing, unclear or contradictory.",
    body: [
      "16 checks scan the graph for the problems people miss: contradictions between documents, missing pieces, vague wording, open questions and assumptions nobody confirmed. Every finding comes with its evidence and goes to the person who can answer it.",
    ],
  },
  {
    step: 3,
    kind: "Plan",
    short: "Plan it",
    title: "Plan turns the graph into work that agents build in parallel.",
    body: [
      "Plan turns the graph into stories and tasks and tracks the state of each one. An item is ready when everything it depends on is finished and no open question blocks it. Coding agents claim ready items in parallel, and each item goes to exactly one agent. When a question is answered, the work it blocked becomes ready on its own.",
    ],
    hint: "Try it: run the next round, or answer the open question, and watch the work move.",
  },
];

/** The accordion of three steps and the figure it drives. One step is always open. */
export function HowItWorks(): JSX.Element {
  const [step, setStep] = useState<StageStep>(1);
  const [replay, setReplay] = useState(0);
  const [plan, setPlan] = useState<PlanState>(initialPlan);
  const [lit, setLit] = useState(0);

  const open = (next: StageStep) => {
    if (next === step) return;
    if (next === 1) setReplay((n) => n + 1);
    if (next === 3) setPlan(initialPlan());
    setLit(0);
    setStep(next);
  };

  useEffect(() => {
    if (step !== 2 || prefersReducedMotion()) return undefined;
    const timer = setInterval(() => setLit((i) => (i + 1) % METHODS.length), CHIP_MS);
    return () => clearInterval(timer);
  }, [step]);

  const current = STEPS[step - 1];
  return (
    <div className="how">
      <div className="how-steps" aria-label="How Graphico works">
        {STEPS.map((s) => (
          <div className="how-step" key={s.step}>
            <button
              className="tab"
              type="button"
              id={`hw${s.step}`}
              aria-controls={`hp${s.step}`}
              aria-expanded={s.step === step}
              onClick={() => open(s.step)}
            >
              <span className="n">{s.step}</span>
              <span className="k">{s.kind}</span>
              <span className="t">{s.short}</span>
            </button>
            <div className="how-copy" id={`hp${s.step}`} role="region" aria-labelledby={`hw${s.step}`} hidden={s.step !== step}>
              <h3>{s.title}</h3>
              {s.body.map((text) => (
                <p key={text}>{text}</p>
              ))}
              {s.chips ? (
                <div className="chips">
                  {s.chips.map((chip) => (
                    <span className="chip" key={chip}>
                      {chip}
                    </span>
                  ))}
                </div>
              ) : null}
              {s.hint ? <p className="small muted">{s.hint}</p> : null}
            </div>
          </div>
        ))}
      </div>

      <div className="panel-box">
        <div className="stage-bar">
          <span className="gx-eyebrow" id="stage-title">
            Step {step} · {current.kind}
          </span>
          <Button variant="ghost" size="sm" onClick={() => open(step === 3 ? 1 : ((step + 1) as StageStep))}>
            {step === 3 ? "Back to step 1" : "Next step"}
          </Button>
        </div>
        <HowStage step={step} replay={replay} plan={plan} lit={lit} titleId="stage-title" />
        <HowLegend step={step} />
        <HowControls
          step={step}
          plan={plan}
          actions={{
            onRound: () => setPlan(nextRound),
            onAnswer: () => setPlan(answerQuestion),
            onReset: () => setPlan(initialPlan()),
          }}
        />
      </div>
    </div>
  );
}
