"use client";
// GitHub: connect with the caller's own account (per-user OAuth, as in the GitHub tab), bind it to the project, then pick the project's
// repositories of the one organization the app is installed on. A repository added here is cloned (the build does not read it yet, D19);
// its clone word shows beside it. Choosing which repositories the app may read happens on GitHub (the install page).
import { useCallback, useEffect, useState } from "react";
import { Button, Notice, PickList, PickRow, Row } from "@graphico/ui";
import {
  cloneViewOf,
  githubFailureOf,
  isCloning,
  pickRowsOf,
  startPolling,
  withoutRepository,
  withRepository,
} from "@/components/settings/github-logic";
import { useToast } from "@/components/ui/Toast";
import type { GitHubConnection, GitHubRepositoryList, ProjectRepositories, ProjectRepository } from "@/lib/domain/github";
import { HowTo } from "../HowTo";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "../canvas/canvas-copy";
import { V2_COPY } from "../v2-copy";
import { SetupFrame } from "./SetupFrame";
import type { SetupProps } from "./setup-props";

interface Loaded {
  connection: GitHubConnection;
  list: ProjectRepositories;
  available: GitHubRepositoryList | "failed" | null;
}

const NO_REPOSITORIES: ProjectRepositories = { enabled: true, canManage: true, repositories: [] };

export function GitHubSetup(props: Readonly<SetupProps>) {
  const { github } = useV2Ports();
  const toast = useToast();
  const { integration: i, projectId } = props;
  const [data, setData] = useState<Loaded | null>(null);
  const [busy, setBusy] = useState(false);
  const bound = i.connectedBy !== null || i.workspace !== null;
  const working = bound && i.health === "active";

  const load = useCallback(() => {
    Promise.all([github.connection(), github.list(projectId).catch(() => NO_REPOSITORIES)])
      .then(([connection, list]) => {
        // Another member's account holds the project when the caller's own connection is not active: only the added repositories show.
        const asked =
          working && connection.status === "active" ? github.available().catch((): "failed" => "failed") : Promise.resolve(null);
        return asked.then((available): Loaded => ({ connection, list, available }));
      })
      .then(setData, () => {
        setData(null);
      });
  }, [github, projectId, working]);
  useEffect(() => {
    load();
  }, [load]);

  const cloning = data !== null && isCloning(data.list);
  useEffect(() => startPolling(cloning, load), [cloning, load]);

  const fail = (e: unknown) => {
    setBusy(false);
    const f = githubFailureOf(e);
    if (f.kind === "gone") {
      props.onError(e);
      return;
    }
    toast({ title: f.text, tone: "danger" });
    if (f.kind === "reload") load();
  };
  const settle = (update: (d: Loaded) => Loaded) => {
    setBusy(false);
    setData((d) => (d === null ? d : update(d)));
  };

  const connect = () => {
    setBusy(true);
    if (data?.connection.status === "active") {
      void props.onConnect().then(() => {
        setBusy(false);
      });
      return;
    }
    github.authorize(props.returnTo).then((url) => {
      window.location.assign(url); // the page is left; it returns with ?github=<code>
    }, fail);
  };
  const choose = () => {
    setBusy(true);
    github.install(props.returnTo).then((url) => {
      window.location.assign(url); // returns with ?github=installed
    }, fail);
  };
  const add = (githubRepoId: number) => {
    setBusy(true);
    github.add(projectId, githubRepoId).then((repo) => {
      settle((d) => ({ ...d, list: withRepository(d.list, repo) }));
    }, fail);
  };
  const remove = (repo: ProjectRepository) => {
    setBusy(true);
    github.remove(projectId, repo.id).then(() => {
      settle((d) => ({ ...d, list: withoutRepository(d.list, repo.id) }));
    }, fail);
  };

  const connectLabel = bound ? V2_COPY.reconnectGithub : V2_COPY.connectGithub;
  return (
    <SetupFrame props={props}>
      <HowTo steps={working ? CANVAS_COPY.howGithubInstall : CANVAS_COPY.howGithubSetup} />
      {working ? (
        <Repositories data={data} busy={busy} onAdd={add} onRemove={remove} onChoose={choose} />
      ) : (
        <Row>
          <Button onClick={connect} disabled={busy || data === null}>
            {busy ? V2_COPY.connecting : connectLabel}
          </Button>
        </Row>
      )}
    </SetupFrame>
  );
}

interface RepositoriesProps {
  data: Loaded | null;
  busy: boolean;
  onAdd: (githubRepoId: number) => void;
  onRemove: (repo: ProjectRepository) => void;
  onChoose: () => void;
}

const noteOf = (current: ProjectRepository | undefined, blocked: "added" | "too_large" | null): string | undefined => {
  if (current !== undefined) return cloneViewOf(current).word;
  return blocked === "too_large" ? V2_COPY.tooLarge : undefined;
};

function Repositories({ data, busy, onAdd, onRemove, onChoose }: Readonly<RepositoriesProps>) {
  if (data === null) return <Notice>{V2_COPY.loading}</Notice>;
  const added = new Map(data.list.repositories.map((r) => [r.githubRepoId, r]));
  const offered = data.available !== null && data.available !== "failed" ? data.available.repositories : [];
  const rows = pickRowsOf(offered, "", new Set()); // sorted, with the ones over the size cap marked
  const extra = data.list.repositories.filter((r) => !offered.some((o) => o.githubRepoId === r.githubRepoId)); // added, not offered now
  const empty = rows.length + extra.length === 0;
  return (
    <>
      {data.available === "failed" ? <Notice role="alert">{V2_COPY.repositoriesFailed}</Notice> : null}
      {empty && data.available !== "failed" ? <Notice>{V2_COPY.noRepositories}</Notice> : null}
      {empty ? null : (
        <PickList legend={V2_COPY.repositories}>
          {rows.map(({ repo, blocked }) => {
            const current = added.get(repo.githubRepoId);
            return (
              <PickRow
                key={repo.githubRepoId}
                label={repo.fullName}
                note={noteOf(current, blocked)}
                checked={current !== undefined}
                disabled={busy || (current === undefined && blocked !== null)}
                onChange={(on) => {
                  if (on) onAdd(repo.githubRepoId);
                  else if (current !== undefined) onRemove(current);
                }}
              />
            );
          })}
          {extra.map((repo) => (
            <PickRow
              key={repo.id}
              label={repo.fullName}
              note={cloneViewOf(repo).word}
              checked
              disabled={busy}
              onChange={() => {
                onRemove(repo);
              }}
            />
          ))}
        </PickList>
      )}
      <Row>
        <Button variant="ghost" size="sm" onClick={onChoose} disabled={busy}>
          {V2_COPY.chooseRepositories}
        </Button>
      </Row>
    </>
  );
}
