"use client";
// Notion plan destination: the link of the page the plan database is created under, Check access, then Add destination (docs/notion/plan.md
// "In the canvas"). Any member may add it; it acts as the member Notion is connected by. A changed link is a new check. A refusal is shown in
// words; an unbound project or an expired connection comes back from the check as a closed reason, and from the save as a 409.
import { useEffect, useId, useState } from "react";
import { Button, Field, Notice, TextInput } from "@graphico/ui";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { NOTION_PLAN_WORDS, notionAccessOutcome, notionPlanCheckText, notionPlanFailureText } from "./notion-plan-logic";
import { ReadGate, readGateState } from "./ReadGate";
import { useCheckAdd } from "./use-check-add";

type Gate = { kind: "loading" } | { kind: "failed" } | { kind: "closed"; text: string } | { kind: "open" };

/** The field and the check of the form, once the server can run the destination. */
export function NotionPlanFormBody({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { notionPlan } = useV2Ports();
  const [link, setLink] = useState("");
  const linkId = useId();
  const trimmed = link.trim();
  const flow = useCheckAdd<{ parentPageTitle: string }>({
    signature: trimmed,
    blank: trimmed === "",
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: async () => notionAccessOutcome(await notionPlan.checkAccess(projectId, trimmed)),
    add: async () => {
      await notionPlan.save(projectId, { link: trimmed, enabled: true });
    },
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: notionPlanFailureText,
  });
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || trimmed === ""} aria-busy={flow.busy !== null || undefined}>
          {flow.label}
        </Button>
      }
    >
      <Field label={CANVAS_COPY.notionPlanParent} htmlFor={linkId} hint={NOTION_PLAN_WORDS.parentHint}>
        <TextInput
          id={linkId}
          value={link}
          spellCheck={false}
          readOnly={flow.busy !== null}
          onChange={(event) => {
            setLink(event.target.value);
          }}
        />
      </Field>
      <div role="status">{flow.result === null ? null : <Notice>{notionPlanCheckText(flow.result)}</Notice>}</div>
      {flow.problem === null ? null : <Notice role="alert">{flow.problem}</Notice>}
    </ConnectFrame>
  );
}

const gateText = (gate: Gate): string => {
  if (gate.kind === "closed") return gate.text;
  return gate.kind === "failed" ? V2_COPY.notionPlanReadFailed : V2_COPY.loading;
};

/** The form for what the port says: the field, or one line when the server cannot run the destination. */
export function NotionPlanForm(props: Readonly<FormProps>) {
  const { notionPlan } = useV2Ports();
  const [gate, setGate] = useState<Gate>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const { projectId } = props;
  useEffect(() => {
    let alive = true;
    notionPlan.get(projectId).then(
      (settings) => {
        if (alive) setGate(settings.available ? { kind: "open" } : { kind: "closed", text: V2_COPY.notionPlanUnavailable });
      },
      () => {
        if (alive) setGate({ kind: "failed" });
      },
    );
    return () => {
      alive = false;
    };
  }, [notionPlan, projectId, attempt]);
  if (gate.kind === "open") return <NotionPlanFormBody {...props} />;
  return (
    <ReadGate
      state={readGateState(gate.kind)}
      text={gateText(gate)}
      onBack={props.onBack}
      onRetry={() => {
        setGate({ kind: "loading" });
        setAttempt((n) => n + 1);
      }}
    />
  );
}
