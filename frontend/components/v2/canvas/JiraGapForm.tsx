"use client";
// Jira gap sub-tasks: the space key and the grouping, a check of access (as the member Atlassian is connected by), then Add channel. Any member
// may add it. The open questions are posted in the background once it is saved; the node counts what was posted and answered.
import { useEffect, useId, useState } from "react";
import { Button, Field, Notice, Select, TextInput } from "@graphico/ui";
import { failureOf, outcomeOf, phaseOf, targetOf, type Outcome } from "@/components/settings/jira-logic";
import type { JiraGapGrouping } from "@/lib/domain/jira-gaps";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { ReadGate, readGateState } from "./ReadGate";

type Busy = "check" | "save" | null;

/** What `jiraGaps.get` says: nothing yet, the read failed, the server cannot run the channel, or the form may be used. */
export type JiraGapGate = "loading" | "failed" | "unavailable" | "open";

const GATE_TEXT: Record<Exclude<JiraGapGate, "open">, string> = {
  loading: V2_COPY.loading,
  failed: V2_COPY.jiraGapsReadFailed,
  unavailable: V2_COPY.jiraGapsUnavailable,
};

const GROUPING_TEXT: Record<JiraGapGrouping, string> = { one: CANVAS_COPY.jiraGapGroupingOne, area: CANVAS_COPY.jiraGapGroupingArea };

const isGrouping = (v: string): v is JiraGapGrouping => v === "one" || v === "area";

/** The fields and the check of the form, once the server can run the channel. */
export function JiraGapFormBody({ projectId, spec, reload, onBack, onClose, pollSeconds }: Readonly<FormProps & { pollSeconds: number }>) {
  const { jiraGaps } = useV2Ports();
  const [project, setProject] = useState("");
  const [grouping, setGrouping] = useState<JiraGapGrouping>("one");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const spaceId = useId();
  const groupingId = useId();
  const check = spec.check ?? { test: "", testing: "", add: "" };
  const phase = phaseOf(outcome, busy);
  const label = { check: check.test, checking: check.testing, save: check.add, saving: CANVAS_COPY.jiraSaving }[phase];
  const key = targetOf(project, "").jiraProject;

  const edit = (apply: () => void) => {
    apply();
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
    if (busy !== null || key === "") return;
    if (phase === "save") {
      setBusy("save");
      jiraGaps.save(projectId, { jiraProject: key, grouping, enabled: true }).then(() => {
        reload.current();
        onClose();
      }, fail);
      return;
    }
    setBusy("check");
    jiraGaps.checkAccess(projectId, key).then((access) => {
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
        <Button type="submit" disabled={busy !== null || key === ""} aria-busy={busy !== null || undefined}>
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
            edit(() => {
              setProject(e.target.value);
            });
          }}
        />
      </Field>
      <Field label={CANVAS_COPY.jiraGapGrouping} htmlFor={groupingId} hint={CANVAS_COPY.jiraGapNote.replace("{n}", String(pollSeconds))}>
        <Select
          id={groupingId}
          value={grouping}
          onChange={(e) => {
            const next = e.target.value;
            if (isGrouping(next))
              edit(() => {
                setGrouping(next);
              });
          }}
        >
          {(Object.keys(GROUPING_TEXT) as JiraGapGrouping[]).map((g) => (
            <option key={g} value={g}>
              {GROUPING_TEXT[g]}
            </option>
          ))}
        </Select>
      </Field>
      <div role="status">
        {outcome?.kind === "ok" ? <Notice>{`${CANVAS_COPY.jiraSpaceLine} ${outcome.projectName}.`}</Notice> : null}
        {texts.alert === undefined || texts.alert === "" ? null : <Notice role="alert">{texts.alert}</Notice>}
      </div>
    </ConnectFrame>
  );
}

/** The form for what `jiraGaps.get` says: the fields, or one line when the server cannot run the channel; a failed read offers Try again. */
export function JiraGapForm(props: Readonly<FormProps>) {
  const { jiraGaps } = useV2Ports();
  const [gate, setGate] = useState<JiraGapGate>("loading");
  const [pollSeconds, setPollSeconds] = useState(60);
  const [attempt, setAttempt] = useState(0);
  const { projectId } = props;
  useEffect(() => {
    let alive = true;
    jiraGaps.get(projectId).then(
      (s) => {
        if (!alive) return;
        setPollSeconds(s.pollSeconds);
        setGate(s.available ? "open" : "unavailable");
      },
      () => {
        if (alive) setGate("failed");
      },
    );
    return () => {
      alive = false;
    };
  }, [jiraGaps, projectId, attempt]);
  if (gate === "open") return <JiraGapFormBody {...props} pollSeconds={pollSeconds} />;
  return (
    <ReadGate
      state={readGateState(gate)}
      text={GATE_TEXT[gate]}
      onBack={props.onBack}
      onRetry={() => {
        setGate("loading");
        setAttempt((n) => n + 1);
      }}
    />
  );
}
