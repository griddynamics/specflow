"use client";
// GitHub Projects plan destination: the repository (one of the project's), the organization project (picked from its open projects, or a new one
// named after this project), a check of access, then Add destination. Any member may add it; it acts as the member GitHub is connected by.
// A changed repository or project is a new check. A refusal is shown in words; `github_permissions_missing` says what to approve.
import { useEffect, useId, useMemo, useState } from "react";
import { Button, Field, Notice, Select } from "@graphico/ui";
import type { ProjectRepository } from "@/lib/domain/github";
import type { GitHubProjectList, GitHubProjectsTarget } from "@/lib/domain/github-projects";
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { V2_COPY } from "../v2-copy";
import { CANVAS_COPY } from "./canvas-copy";
import { ConnectFrame } from "./ConnectFrame";
import { accessOutcome, githubPlanCheckText, githubPlanFailureText } from "./github-plan-logic";
import { ReadGate, readGateState } from "./ReadGate";
import { useCheckAdd } from "./use-check-add";

const NEW = "new";

type Gate =
  | { kind: "loading" }
  | { kind: "failed" }
  | { kind: "closed"; text: string }
  | { kind: "open"; repositories: ProjectRepository[]; name: string | null };

type Projects = { kind: "loading" } | { kind: "failed" } | { kind: "ready"; list: GitHubProjectList };

/** What `githubProjects.projects` says of the repository picked: the organization's open projects, or why they cannot be read. */
function useProjectList(projectId: string, repositoryId: string): Projects {
  const { githubProjects } = useV2Ports();
  const [read, setRead] = useState<{ repositoryId: string; value: Projects } | null>(null);
  useEffect(() => {
    let alive = true;
    githubProjects.projects(projectId, repositoryId).then(
      (list) => {
        if (alive) setRead({ repositoryId, value: { kind: "ready", list } });
      },
      () => {
        if (alive) setRead({ repositoryId, value: { kind: "failed" } });
      },
    );
    return () => {
      alive = false;
    };
  }, [githubProjects, projectId, repositoryId]);
  return read?.repositoryId === repositoryId ? read.value : { kind: "loading" };
}

/** The fields and the check of the form, once the project has a repository and the server can run the destination. */
export function GitHubPlanFormBody({
  projectId,
  spec,
  reload,
  onBack,
  onClose,
  repositories,
  name,
}: Readonly<FormProps & { repositories: readonly ProjectRepository[]; name: string | null }>) {
  const { githubProjects } = useV2Ports();
  const [repositoryId, setRepositoryId] = useState(repositories[0]?.id ?? "");
  const [choice, setChoice] = useState(name === null ? "" : NEW);
  const repoFieldId = useId();
  const projectFieldId = useId();
  const projects = useProjectList(projectId, repositoryId);
  const list = projects.kind === "ready" ? projects.list : null;
  const person = list !== null && list.organization === null;
  const target = useMemo<GitHubProjectsTarget | null>(() => {
    if (repositoryId === "" || choice === "") return null;
    if (choice === NEW) return name === null ? null : { repositoryId, project: { newTitle: name } };
    return { repositoryId, project: { nodeId: choice } };
  }, [repositoryId, choice, name]);
  const flow = useCheckAdd<{ organization: string; projectTitle: string | null }>({
    signature: `${repositoryId}|${choice}`,
    blank: target === null || person,
    labels: spec.check ?? { test: "", testing: "", add: "" },
    check: async () => accessOutcome(await githubProjects.checkAccess(projectId, defined(target))),
    add: async () => {
      await githubProjects.save(projectId, { ...defined(target), enabled: true });
    },
    onAdded: () => {
      reload.current();
      onClose();
    },
    failureText: githubPlanFailureText,
  });
  const choices = list?.projects ?? [];
  return (
    <ConnectFrame
      onBack={onBack}
      onSubmit={(event) => {
        event.preventDefault();
        flow.run();
      }}
      action={
        <Button type="submit" disabled={flow.busy !== null || target === null || person} aria-busy={flow.busy !== null || undefined}>
          {flow.label}
        </Button>
      }
    >
      <Field label={CANVAS_COPY.githubPlanRepository} htmlFor={repoFieldId}>
        <Select
          id={repoFieldId}
          value={repositoryId}
          disabled={flow.busy !== null}
          onChange={(e) => {
            setRepositoryId(e.target.value);
            setChoice(name === null ? "" : NEW);
          }}
        >
          {repositories.map((r) => (
            <option key={r.id} value={r.id}>
              {r.fullName}
            </option>
          ))}
        </Select>
      </Field>
      <Field
        label={CANVAS_COPY.githubPlanProject}
        htmlFor={projectFieldId}
        hint={choice === NEW ? CANVAS_COPY.githubPlanNewHint : undefined}
      >
        <Select
          id={projectFieldId}
          value={choice}
          disabled={flow.busy !== null}
          onChange={(e) => {
            setChoice(e.target.value);
          }}
        >
          <option value={name === null ? "" : NEW}>
            {name === null ? CANVAS_COPY.githubPlanPick : CANVAS_COPY.githubPlanNew.replace("{name}", name)}
          </option>
          {choices.map((p) => (
            <option key={p.nodeId} value={p.nodeId}>
              {p.title}
            </option>
          ))}
        </Select>
      </Field>
      <div role="status">
        {projects.kind === "loading" ? <Notice>{CANVAS_COPY.githubPlanLoadingProjects}</Notice> : null}
        {person ? <Notice>{CANVAS_COPY.githubPlanUser}</Notice> : null}
        {flow.result !== null && target !== null ? <Notice>{githubPlanCheckText(flow.result, target)}</Notice> : null}
      </div>
      {projects.kind === "failed" ? <Notice role="alert">{CANVAS_COPY.githubPlanListFailed}</Notice> : null}
      {flow.problem === null ? null : <Notice role="alert">{flow.problem}</Notice>}
    </ConnectFrame>
  );
}

function defined<T>(v: T | null): T {
  if (v === null) throw new Error("no target");
  return v;
}

const gateText = (gate: Gate): string => {
  if (gate.kind === "closed") return gate.text;
  return gate.kind === "failed" ? V2_COPY.githubPlanReadFailed : V2_COPY.loading;
};

/** The form for what the ports say: the fields, or one line when there is no repository yet or the server cannot run the destination. */
export function GitHubPlanForm(props: Readonly<FormProps>) {
  const { github, githubProjects, projects } = useV2Ports();
  const [gate, setGate] = useState<Gate>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const { projectId } = props;
  useEffect(() => {
    let alive = true;
    Promise.all([
      githubProjects.get(projectId),
      github.list(projectId),
      projects.get(projectId).then(
        (p) => p.name,
        () => null,
      ),
    ]).then(
      ([settings, list, name]) => {
        if (!alive) return;
        if (!settings.available) setGate({ kind: "closed", text: V2_COPY.githubPlanUnavailable });
        else if (list.repositories.length === 0) setGate({ kind: "closed", text: CANVAS_COPY.githubPlanNoRepositories });
        else setGate({ kind: "open", repositories: list.repositories, name });
      },
      () => {
        if (alive) setGate({ kind: "failed" });
      },
    );
    return () => {
      alive = false;
    };
  }, [github, githubProjects, projects, projectId, attempt]);
  if (gate.kind === "open") return <GitHubPlanFormBody {...props} repositories={gate.repositories} name={gate.name} />;
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
