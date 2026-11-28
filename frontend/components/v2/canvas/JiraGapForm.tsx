"use client";
// Jira gap sub-tasks: the space key and the grouping, a check of access (as the member Atlassian is connected by), then Add channel. Any member
// may add it. The open questions are posted in the background once it is saved; the node counts what was posted and answered.
import { useEffect, useId, useState } from "react";
import { Button, Field, Notice, Select } from "@graphico/ui";
import { outcomeOf, phaseOf, targetOf, type Outcome } from "@/components/shared/settings/jira-logic";
import type { JiraGapGrouping } from "@/lib/domain/jira-gaps";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { ReadGate, readGateState } from "./ReadGate";
import { problemOf } from "./jira-outcome";
import { JiraSpaceField } from "./JiraSpaceField";
import { signedFor, type Signed } from "./signed";

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
  const [held, setHeld] = useState<Signed<Outcome> | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const spaceId = useId();
  const groupingId = useId();
  const check = spec.check ?? { test: "", testing: "", add: "" };
  const signature = `${targetOf(project, "").jiraProject}|${grouping}`;
  const outcome = signedFor(held, signature);
  const phase = phaseOf(outcome, busy);
  const label = { check: check.test, checking: check.testing, save: check.add, saving: CANVAS_COPY.jiraSaving }[phase];
  const key = targetOf(project, "").jiraProject;

  // An answer belongs to what was typed when it was asked for (`signature`): a late one never shows under another key or grouping.
  const fail = (sent: string) => (e: unknown) => {
    setBusy(null);
    setHeld({ signature: sent, value: problemOf(e) });
  };
  const run = () => {
    if (busy !== null || key === "") return;
    if (phase === "save") {
      setBusy("save");
      jiraGaps.save(projectId, { jiraProject: key, grouping, enabled: true }).then(() => {
        reload.current();
        onClose();
      }, fail(signature));
      return;
    }
    setBusy("check");
    jiraGaps.checkAccess(projectId, key).then((access) => {
      setBusy(null);
      setHeld({ signature, value: outcomeOf(access, true) });
    }, fail(signature));
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
      <JiraSpaceField id={spaceId} value={project} busy={busy !== null} error={texts.project} onChange={setProject} />
      <Field label={CANVAS_COPY.jiraGapGrouping} htmlFor={groupingId} hint={CANVAS_COPY.jiraGapNote.replace("{n}", String(pollSeconds))}>
        <Select
          id={groupingId}
          value={grouping}
          disabled={busy !== null}
          onChange={(e) => {
            const next = e.target.value;
            if (isGrouping(next)) setGrouping(next);
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
