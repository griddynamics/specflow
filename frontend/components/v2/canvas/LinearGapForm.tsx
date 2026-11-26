"use client";
// Linear gap sub-issues: the team key, an optional Linear project and the grouping, a check of access (as the member Linear is connected by),
// then Add channel. Any member may add it. The open questions are posted in the background once it is saved; the node counts what was posted
// and answered.
import { useEffect, useId, useState } from "react";
import { Button, Field, Notice, Select, TextInput } from "@graphico/ui";
import { isDataError } from "@/lib/domain/errors";
import type { LinearGapGrouping, LinearGapsAccess } from "@/lib/domain/linear-gaps";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { ConnectFrame } from "./ConnectFrame";
import { LINEAR_GAP_COPY as COPY } from "./linear-gap-copy";
import { ReadGate, readGateState } from "./ReadGate";
import { signedFor, type Signed } from "./signed";

type Busy = "check" | "save" | null;
type Phase = "check" | "checking" | "save" | "saving";

/** What `linearGaps.get` says: nothing yet, the read failed, the server cannot run the channel, or the form may be used. */
export type LinearGapGate = "loading" | "failed" | "unavailable" | "open";

/** What the last check or save left on the form: the names of a passed check, or the texts of a failure. */
export type Outcome = { kind: "ok"; teamName: string; projectName: string | null } | { kind: "problem"; texts: Problem };
export interface Problem {
  team?: string;
  project?: string;
  alert?: string;
}

const GATE_TEXT: Record<Exclude<LinearGapGate, "open">, string> = {
  loading: V2_COPY.loading,
  failed: COPY.readFailed,
  unavailable: V2_COPY.linearGapsUnavailable,
};

const GROUPING_TEXT: Record<LinearGapGrouping, string> = { one: COPY.groupingOne, area: COPY.groupingArea };

const FIELD_AT = { team_key: "team", linear_project: "project" } as const;

const isGrouping = (v: string): v is LinearGapGrouping => v === "one" || v === "area";

/** The words under the field a code names, or the alert line. */
function textsOf(code: string, field: "team" | "project" | null): Problem {
  const at = field ?? COPY.reasons[code];
  if (at === "team") return { team: code === "linear_team_not_found" ? COPY.teamNotFound : COPY.keyInvalid };
  if (at === "project") return { project: COPY.projectNotFound };
  return { alert: COPY.errors[code] ?? COPY.generic };
}

export function outcomeOf(access: LinearGapsAccess): Outcome {
  if (access.result === "ok") return { kind: "ok", teamName: access.teamName, projectName: access.linearProjectName };
  const field = access.field === null ? null : FIELD_AT[access.field];
  return { kind: "problem", texts: textsOf(access.reason, field) };
}

/** What a failed call leaves on the form: the texts under the fields the API named, or one alert line. */
export function problemOf(e: unknown): Outcome {
  if (!isDataError(e)) return { kind: "problem", texts: { alert: COPY.generic } };
  const entry = e.detail?.errors?.[0];
  if (entry?.loc[1] === "team_key") return { kind: "problem", texts: { team: COPY.keyInvalid } };
  return { kind: "problem", texts: textsOf(e.detail?.apiCode ?? "", null) };
}

export function phaseOf(outcome: Outcome | null, busy: Busy): Phase {
  if (busy === "check") return "checking";
  if (busy === "save") return "saving";
  return outcome?.kind === "ok" ? "save" : "check";
}

/** What a passed check names: the team, and the project when there is one. */
function passedLine(o: { teamName: string; projectName: string | null }): string {
  const project = o.projectName === null ? "" : `, ${COPY.projectLine} ${o.projectName}`;
  return `${COPY.teamLine} ${o.teamName}${project}.`;
}

/** The fields and the check of the form, once the server can run the channel. */
export function LinearGapFormBody({ projectId, spec, reload, onBack, onClose }: Readonly<FormProps>) {
  const { linearGaps } = useV2Ports();
  const [team, setTeam] = useState("");
  const [project, setProject] = useState("");
  const [grouping, setGrouping] = useState<LinearGapGrouping>("one");
  const [held, setHeld] = useState<Signed<Outcome> | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const teamId = useId();
  const projectFieldId = useId();
  const groupingId = useId();
  const check = spec.check ?? { test: "", testing: "", add: "" };
  const key = team.trim().toUpperCase();
  const where = project.trim() === "" ? null : project.trim();
  const signature = `${key}|${where ?? ""}|${grouping}`;
  const outcome = signedFor(held, signature);
  const phase = phaseOf(outcome, busy);
  const label = { check: check.test, checking: check.testing, save: check.add, saving: COPY.saving }[phase];

  // An answer belongs to what was typed when it was asked for (`signature`): a late one never shows under another team or grouping.
  const fail = (sent: string) => (e: unknown) => {
    setBusy(null);
    setHeld({ signature: sent, value: problemOf(e) });
  };
  const run = () => {
    if (busy !== null || key === "") return;
    if (phase === "save") {
      setBusy("save");
      linearGaps.save(projectId, { teamKey: key, linearProject: where, grouping, enabled: true }).then(() => {
        reload.current();
        onClose();
      }, fail(signature));
      return;
    }
    setBusy("check");
    linearGaps.checkAccess(projectId, { teamKey: key, linearProject: where }).then((access) => {
      setBusy(null);
      setHeld({ signature, value: outcomeOf(access) });
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
      <Field label={COPY.team} htmlFor={teamId} hint={COPY.teamHint} error={texts.team}>
        <TextInput
          id={teamId}
          value={team}
          spellCheck={false}
          readOnly={busy !== null}
          onChange={(e) => {
            setTeam(e.target.value);
          }}
        />
      </Field>
      <Field label={COPY.project} htmlFor={projectFieldId} hint={COPY.projectHint} error={texts.project}>
        <TextInput
          id={projectFieldId}
          value={project}
          spellCheck={false}
          readOnly={busy !== null}
          onChange={(e) => {
            setProject(e.target.value);
          }}
        />
      </Field>
      <Field label={COPY.grouping} htmlFor={groupingId} hint={COPY.note}>
        <Select
          id={groupingId}
          value={grouping}
          disabled={busy !== null}
          onChange={(e) => {
            const next = e.target.value;
            if (isGrouping(next)) setGrouping(next);
          }}
        >
          {(Object.keys(GROUPING_TEXT) as LinearGapGrouping[]).map((g) => (
            <option key={g} value={g}>
              {GROUPING_TEXT[g]}
            </option>
          ))}
        </Select>
      </Field>
      <div role="status">
        {outcome?.kind === "ok" ? <Notice>{passedLine(outcome)}</Notice> : null}
        {texts.alert === undefined || texts.alert === "" ? null : <Notice role="alert">{texts.alert}</Notice>}
      </div>
    </ConnectFrame>
  );
}

/** The form for what `linearGaps.get` says: the fields, or one line when the server cannot run the channel; a failed read offers Try again. */
export function LinearGapForm(props: Readonly<FormProps>) {
  const { linearGaps } = useV2Ports();
  const [gate, setGate] = useState<LinearGapGate>("loading");
  const [attempt, setAttempt] = useState(0);
  const { projectId } = props;
  useEffect(() => {
    let alive = true;
    linearGaps.get(projectId).then(
      (s) => {
        if (alive) setGate(s.available ? "open" : "unavailable");
      },
      () => {
        if (alive) setGate("failed");
      },
    );
    return () => {
      alive = false;
    };
  }, [linearGaps, projectId, attempt]);
  if (gate === "open") return <LinearGapFormBody {...props} />;
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
