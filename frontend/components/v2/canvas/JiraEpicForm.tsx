"use client";
// Jira epic: an epic key (`ABC-12`) or its link on the project's Atlassian site, a check that counts the issues under it, then Add source (one
// source per epic, D5; the epic and its issues with their comments are read once, D7). Same flow and same acting account as the Confluence link.
import { useId, useState } from "react";
import { Button, Field, Notice, TextInput } from "@graphico/ui";
import type { JiraEpicCheck } from "@/lib/domain/sources";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { jiraEpicCheckText, jiraEpicFailureText } from "./jira-epic-logic";
import { ConnectFrame } from "./ConnectFrame";
import { useCheckAdd } from "./use-check-add";

export function JiraEpicForm({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const [key, setKey] = useState("");
  const keyId = useId();
  const typed = key.trim();
  const flow = useCheckAdd<JiraEpicCheck>({
    signature: typed,
    blank: typed === "",
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: () => sources.checkJiraEpic(projectId, typed),
    add: () => sources.addJiraEpic(projectId, typed),
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: jiraEpicFailureText,
  });
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || typed === ""} aria-busy={flow.busy !== null || undefined}>
          {flow.label}
        </Button>
      }
    >
      <Field label={CANVAS_COPY.jiraEpicSource} htmlFor={keyId} hint={CANVAS_COPY.jiraEpicSourceHint}>
        <TextInput
          id={keyId}
          value={key}
          spellCheck={false}
          readOnly={flow.busy !== null}
          onChange={(event) => {
            setKey(event.target.value);
          }}
        />
      </Field>
      <div role="status">{flow.result === null ? null : <Notice>{jiraEpicCheckText(flow.result)}</Notice>}</div>
      {flow.problem === null ? null : <Notice role="alert">{flow.problem}</Notice>}
    </ConnectFrame>
  );
}
