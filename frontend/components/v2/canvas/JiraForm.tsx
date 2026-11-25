"use client";
// Jira plan destination: the space key and an optional epic key, a check of access, then Add destination (the V1 Jira tab's rules,
// `jira-logic.ts`). The target is saved on, and the plan syncs to it once it exists; the acting account is the project's Atlassian integration.
import { useEffect, useId, useState } from "react";
import { Button, Field, Notice, TextInput } from "@graphico/ui";
import { failureOf, outcomeOf, phaseOf, targetOf, type Outcome } from "@/components/settings/jira-logic";
import { COPY } from "@/lib/copy";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";

type Busy = "check" | "save" | null;

/** What `jira.get` says about the caller: nothing yet, the server has no Atlassian account, the caller may not change the target, or may. */
export type JiraGate = "loading" | "failed" | "unavailable" | "read_only" | "open";

export function gateOf(s: { available: boolean; editable: boolean } | null): JiraGate {
  if (s === null) return "loading";
  if (!s.available) return "unavailable";
  return s.editable ? "open" : "read_only";
}

const GATE_TEXT: Record<Exclude<JiraGate, "open">, string> = {
  loading: V2_COPY.loading,
  failed: V2_COPY.jiraReadFailed,
  unavailable: COPY.jiraUnavailable,
  read_only: V2_COPY.readOnlyJira,
};

/** What a passed check says. */
const okText = (o: Extract<Outcome, { kind: "ok" }>, epic: string): string =>
  [`${CANVAS_COPY.jiraSpaceLine} ${o.projectName}.`, o.epicSummary === null ? "" : `${CANVAS_COPY.jiraEpicLine} ${epic}: ${o.epicSummary}.`]
    .filter(Boolean)
    .join(" ");

/** The fields and the check of the form, once the API has said the caller may change the target. */
export function JiraFormBody({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { jira } = useV2Ports();
  const [project, setProject] = useState("");
  const [epic, setEpic] = useState("");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const spaceId = useId();
  const epicId = useId();
  const check = spec.check ?? { test: "", testing: "", add: "" };
  const phase = phaseOf(outcome, busy);
  const label = { check: check.test, checking: check.testing, save: check.add, saving: CANVAS_COPY.jiraSaving }[phase];
  const target = targetOf(project, epic);

  const edit = (set: (v: string) => void) => (v: string) => {
    set(v);
    setOutcome(null);
  };
  const fail = (e: unknown) => {
    const f = failureOf(e);
    setBusy(null);
    setOutcome({
      kind: "problem",
      texts: f.kind === "fields" ? f.texts : { alert: f.kind === "toast" || f.kind === "disconnected" ? f.text : "" },
    });
  };
  const run = () => {
    if (busy !== null || project.trim() === "") return;
    if (phase === "save") {
      setBusy("save");
      jira.save(projectId, { ...target, enabled: true }).then(() => {
        reload.current();
        onClose();
      }, fail);
      return;
    }
    setBusy("check");
    jira.checkAccess(projectId, target).then((access) => {
      setBusy(null);
      setOutcome(outcomeOf(access, true));
    }, fail);
  };
  const texts = outcome?.kind === "problem" ? outcome.texts : {};
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        run();
      }}
      action={
        <Button type="submit" disabled={busy !== null || project.trim() === ""} aria-busy={busy !== null || undefined}>
          {label}
        </Button>
      }
    >
      <Field label={CANVAS_COPY.jiraSpace} htmlFor={spaceId} error={texts.project}>
        <TextInput
          id={spaceId}
          value={project}
          spellCheck={false}
          onChange={(e) => {
            edit(setProject)(e.target.value);
          }}
        />
      </Field>
      <Field label={CANVAS_COPY.jiraEpic} htmlFor={epicId} hint={CANVAS_COPY.jiraEpicHint} error={texts.epic}>
        <TextInput
          id={epicId}
          value={epic}
          spellCheck={false}
          onChange={(e) => {
            edit(setEpic)(e.target.value);
          }}
        />
      </Field>
      <div role="status">
        {outcome?.kind === "ok" ? <Notice>{okText(outcome, target.epicKey ?? "")}</Notice> : null}
        {texts.alert === undefined || texts.alert === "" ? null : <Notice role="alert">{texts.alert}</Notice>}
      </div>
    </ConnectFrame>
  );
}

/**
 * The form for the caller `jira.get` describes: the fields, or one line when the server has no Atlassian account or the API refuses the
 * caller. A read that failed (the network, not an answer) says so and offers to try again: it is not "no Atlassian account".
 */
export function JiraForm(props: Readonly<FormProps>) {
  const { jira } = useV2Ports();
  const [gate, setGate] = useState<JiraGate>("loading");
  const [attempt, setAttempt] = useState(0);
  const { projectId } = props;
  useEffect(() => {
    let alive = true;
    jira.get(projectId).then(
      (settings) => {
        if (alive) setGate(gateOf(settings));
      },
      () => {
        if (alive) setGate("failed");
      },
    );
    return () => {
      alive = false;
    };
  }, [jira, projectId, attempt]);
  if (gate === "open") return <JiraFormBody {...props} />;
  return (
    <ConnectFrame
      onBack={props.onBack}
      action={
        gate === "failed" ? (
          <Button
            onClick={() => {
              setGate("loading");
              setAttempt((n) => n + 1);
            }}
          >
            {V2_COPY.tryAgain}
          </Button>
        ) : undefined
      }
    >
      <Notice role={gate === "failed" ? "alert" : "status"}>{GATE_TEXT[gate]}</Notice>
    </ConnectFrame>
  );
}
