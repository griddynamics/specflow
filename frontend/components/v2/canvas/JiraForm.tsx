"use client";
// Jira plan destination: the space key and an optional epic key, a check of access, then Add destination (the V1 Jira tab's rules,
// `jira-logic.ts`). The target is saved on, and the plan syncs to it once it exists; the acting account is the project's Atlassian integration.
import { useId, useState } from "react";
import { Button, Field, Notice, TextInput } from "@graphico/ui";
import { failureOf, outcomeOf, phaseOf, targetOf, type Outcome } from "@/components/settings/jira-logic";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";

type Busy = "check" | "save" | null;

/** What a passed check says. */
const okText = (o: Extract<Outcome, { kind: "ok" }>, epic: string): string =>
  [`${CANVAS_COPY.jiraSpaceLine} ${o.projectName}.`, o.epicSummary === null ? "" : `${CANVAS_COPY.jiraEpicLine} ${epic}: ${o.epicSummary}.`]
    .filter(Boolean)
    .join(" ");

export function JiraForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
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
