"use client";
// Linear plan destination: the team (one of the connected member's), the Linear project (picked from the team's projects, or a new one named after
// this project), a check of access, then Add destination. Any member may add it; it acts as the member Linear is connected by. A changed team or
// project is a new check. A refusal is shown in words.
import { useEffect, useId, useMemo, useState } from "react";
import { Button, Field, Notice, Select } from "@graphico/ui";
import { isDataError } from "@/lib/domain/errors";
import type { LinearPlanTarget, LinearProjectChoice, LinearTeam } from "@/lib/domain/linear-plan";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { ConnectFrame } from "./ConnectFrame";
import { LINEAR_PLAN_COPY, LINEAR_RECONNECT, linearAccessOutcome, linearPlanCheckText, linearPlanFailureText } from "./linear-plan-logic";
import { ReadGate, readGateState } from "./ReadGate";
import { useCheckAdd } from "./use-check-add";

const NEW = "new";

type Gate =
  { kind: "loading" } | { kind: "failed" } | { kind: "closed"; text: string } | { kind: "open"; teams: LinearTeam[]; name: string | null };

type Projects = { kind: "loading" } | { kind: "failed" } | { kind: "ready"; list: LinearProjectChoice[] };

/** What `linearPlan.projects` says of the team picked: its Linear projects, or that they cannot be read. */
function useProjectList(projectId: string, teamId: string): Projects {
  const { linearPlan } = useV2Ports();
  const [read, setRead] = useState<{ teamId: string; value: Projects } | null>(null);
  useEffect(() => {
    let alive = true;
    linearPlan.projects(projectId, teamId).then(
      (list) => {
        if (alive) setRead({ teamId, value: { kind: "ready", list } });
      },
      () => {
        if (alive) setRead({ teamId, value: { kind: "failed" } });
      },
    );
    return () => {
      alive = false;
    };
  }, [linearPlan, projectId, teamId]);
  return read?.teamId === teamId ? read.value : { kind: "loading" };
}

function defined<T>(v: T | null): T {
  if (v === null) throw new Error("no target");
  return v;
}

/** The fields and the check of the form, once the member has teams and the server can run the destination. */
export function LinearPlanFormBody({
  projectId,
  spec,
  reload,
  onBack,
  onClose,
  teams,
  name,
}: Readonly<FormProps & { teams: readonly LinearTeam[]; name: string | null }>) {
  const { linearPlan } = useV2Ports();
  const [teamId, setTeamId] = useState(teams[0]?.id ?? "");
  const [choice, setChoice] = useState(name === null ? "" : NEW);
  const teamFieldId = useId();
  const projectFieldId = useId();
  const projects = useProjectList(projectId, teamId);
  const target = useMemo<LinearPlanTarget | null>(() => {
    if (teamId === "" || choice === "") return null;
    return { teamId, projectId: choice === NEW ? null : choice };
  }, [teamId, choice]);
  const flow = useCheckAdd<{ teamKey: string; projectName: string | null }>({
    signature: `${teamId}|${choice}`,
    blank: target === null,
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: async () => linearAccessOutcome(await linearPlan.checkAccess(projectId, defined(target))),
    add: async () => {
      await linearPlan.save(projectId, { ...defined(target), enabled: true });
    },
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: linearPlanFailureText,
  });
  const choices = projects.kind === "ready" ? projects.list : [];
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || target === null} aria-busy={flow.busy !== null || undefined}>
          {flow.label}
        </Button>
      }
    >
      <Field label={LINEAR_PLAN_COPY.team} htmlFor={teamFieldId}>
        <Select
          id={teamFieldId}
          value={teamId}
          disabled={flow.busy !== null}
          onChange={(e) => {
            setTeamId(e.target.value);
            setChoice(name === null ? "" : NEW);
          }}
        >
          {teams.map((t) => (
            <option key={t.id} value={t.id}>
              {`${t.name} (${t.key})`}
            </option>
          ))}
        </Select>
      </Field>
      <Field label={LINEAR_PLAN_COPY.project} htmlFor={projectFieldId} hint={choice === NEW ? LINEAR_PLAN_COPY.newProjectHint : undefined}>
        <Select
          id={projectFieldId}
          value={choice}
          disabled={flow.busy !== null}
          onChange={(e) => {
            setChoice(e.target.value);
          }}
        >
          <option value={name === null ? "" : NEW}>
            {name === null ? LINEAR_PLAN_COPY.pickProject : LINEAR_PLAN_COPY.newProject.replace("{name}", name)}
          </option>
          {choices.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </Select>
      </Field>
      <div role="status">
        {projects.kind === "loading" ? <Notice>{LINEAR_PLAN_COPY.loadingProjects}</Notice> : null}
        {flow.result !== null && target !== null ? <Notice>{linearPlanCheckText(flow.result, target, name)}</Notice> : null}
      </div>
      {projects.kind === "failed" ? <Notice role="alert">{LINEAR_PLAN_COPY.projectsFailed}</Notice> : null}
      {flow.problem === null ? null : <Notice role="alert">{flow.problem}</Notice>}
    </ConnectFrame>
  );
}

const gateText = (gate: Gate): string => {
  if (gate.kind === "closed") return gate.text;
  return gate.kind === "failed" ? V2_COPY.linearPlanReadFailed : V2_COPY.loading;
};

/** A refused read the member can fix by connecting Linear is a closed door with its words; any other failure is a failed read. */
const gateOfFailure = (e: unknown): Gate =>
  isDataError(e) && LINEAR_RECONNECT.has(e.detail?.apiCode ?? "") ? { kind: "closed", text: linearPlanFailureText(e) } : { kind: "failed" };

/** The form for what the ports say: the fields, or one line when the server cannot run the destination or Linear is not connected. */
export function LinearPlanForm(props: Readonly<FormProps>) {
  const { linearPlan, projects } = useV2Ports();
  const [gate, setGate] = useState<Gate>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const { projectId } = props;
  useEffect(() => {
    let alive = true;
    const read = async (): Promise<Gate> => {
      const settings = await linearPlan.get(projectId);
      if (!settings.available) return { kind: "closed", text: V2_COPY.linearPlanUnavailable };
      const [list, name] = await Promise.all([
        linearPlan.teams(projectId),
        projects.get(projectId).then(
          (p) => p.name,
          () => null,
        ),
      ]);
      if (list.teams.length === 0) return { kind: "closed", text: LINEAR_PLAN_COPY.noTeams };
      return { kind: "open", teams: list.teams, name };
    };
    read().then(
      (next) => {
        if (alive) setGate(next);
      },
      (e: unknown) => {
        if (alive) setGate(gateOfFailure(e));
      },
    );
    return () => {
      alive = false;
    };
  }, [linearPlan, projects, projectId, attempt]);
  if (gate.kind === "open") return <LinearPlanFormBody {...props} teams={gate.teams} name={gate.name} />;
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
