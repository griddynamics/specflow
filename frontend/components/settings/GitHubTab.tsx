"use client";
// plans/GITHUB-INTEGRATION/contract.md#settings-tab: the GitHub tab. An owner connects their own GitHub account, picks on GitHub the
// repositories the Specflow app may read, and adds them to the project; Specflow clones each one to check access and keeps a copy. Every
// member sees the repositories and their clone status read-only. The rules are in github-logic.ts, the pieces in github-parts.tsx. The
// return routes come back with `?github=<code>`, shown once as a toast and then removed from the URL. The list reloads while a clone
// check waits or runs.
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { isDataError } from "@/lib/domain/errors";
import type { GitHubConnection, GitHubRepositoryList, ProjectRepositories, ProjectRepository } from "@/lib/domain/github";
import { ConfirmDialog } from "./account-parts";
import { AddDialog, GitHubAccount, GitHubUnavailable, Repositories } from "./github-parts";
import {
  githubFailureOf,
  githubRowOf,
  githubToast,
  isCloning,
  startPolling,
  withoutRepository,
  withRepository,
  type GitHubFailure,
} from "./github-logic";
import { LoadFailed } from "./parts";
import { SETTINGS_COPY } from "./settings-copy";
import { useGitHubPort, useSettingsRoutes } from "./use-settings-ports";

interface Loaded {
  connection: GitHubConnection;
  list: ProjectRepositories;
}
type Ask = { kind: "disconnect" } | { kind: "remove"; repo: ProjectRepository };
interface Picker {
  state: GitHubRepositoryList | "loading" | "failed";
  query: string;
  adding: number | null;
}

export function GitHubTab({ projectId }: Readonly<{ projectId: string }>) {
  const github = useGitHubPort();
  const routes = useSettingsRoutes();
  const router = useRouter();
  const toast = useToast();
  const query = useSearchParams();
  const [data, setData] = useState<Loaded | "failed" | null>(null);
  const [linking, setLinking] = useState<"connect" | "disconnect" | "choose" | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(new Set());
  const [picker, setPicker] = useState<Picker | null>(null);
  const toasted = useRef(false); // `?github=` toasts once per mount
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const back = routes.settings(projectId, { tab: "github" });
  const goHome = useCallback(() => {
    router.replace(routes.home());
  }, [router, routes]);

  // The `?github=` answer of a return route: one toast, then the parameter leaves the URL without a reload.
  const githubCode = query.get("github");
  useEffect(() => {
    if (githubCode === null || toasted.current) return;
    toasted.current = true;
    toast(githubToast(githubCode));
    router.replace(back, { scroll: false });
  }, [githubCode, toast, router, back]);

  const load = useCallback(() => {
    Promise.all([github.connection(), github.list(projectId)]).then(
      ([connection, list]) => {
        if (live.current) setData({ connection, list });
      },
      (e: unknown) => {
        if (!live.current) return;
        if (githubFailureOf(e).kind === "gone") goHome();
        else setData((d) => (d === null || d === "failed" ? "failed" : d)); // a failed poll keeps what is shown
      },
    );
  }, [github, projectId, goHome]);
  useEffect(() => {
    load();
  }, [load]);

  const cloning = typeof data === "object" && data !== null && isCloning(data.list);
  useEffect(() => startPolling(cloning, load), [cloning, load]);

  const react = useCallback(
    (f: GitHubFailure) => {
      if (f.kind === "gone") {
        goHome();
        return;
      }
      toast({ title: f.text, tone: "danger" });
      if (f.kind === "reload") load();
    },
    [goHome, toast, load],
  );

  const loadPicker = useCallback(() => {
    setPicker((p) => (p === null ? null : { ...p, state: "loading" }));
    github.available().then(
      (list) => {
        if (live.current) setPicker((p) => (p === null ? null : { ...p, state: list }));
      },
      (e: unknown) => {
        if (!live.current) return;
        const code = isDataError(e) ? e.detail?.apiCode : undefined;
        if (code === "github_not_connected" || code === "github_reconnect_required") {
          setPicker(null);
          react(githubFailureOf(e));
        } else setPicker((p) => (p === null ? null : { ...p, state: "failed" }));
      },
    );
  }, [github, react]);

  if (data === null) {
    return (
      <div aria-hidden data-testid="settings-loading">
        <Skeleton variant="block" height={200} />
      </div>
    );
  }
  if (data === "failed") {
    return (
      <LoadFailed
        text={SETTINGS_COPY.githubLoadFailed}
        onRetry={() => {
          setData(null);
          load();
        }}
      />
    );
  }
  const { connection, list } = data;
  if (!connection.enabled || !list.enabled) return <GitHubUnavailable />;

  /** Leaves the page for GitHub (the authorize page or the app's install page); stays busy until then. */
  const goToGitHub = (kind: "connect" | "choose", request: (returnTo: string) => Promise<string>) => {
    if (linking !== null) return;
    setLinking(kind);
    request(back).then(
      (url) => {
        window.location.assign(url);
      },
      (e: unknown) => {
        if (!live.current) return;
        setLinking(null);
        react(githubFailureOf(e));
      },
    );
  };
  const connect = () => {
    goToGitHub("connect", (r) => github.authorize(r));
  };
  const choose = () => {
    goToGitHub("choose", (r) => github.install(r));
  };

  const withBusy = (id: string, on: boolean) => {
    setBusyIds((ids) => {
      const next = new Set(ids);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };
  const putRepository = (repo: ProjectRepository) => {
    setData((d) => (d === null || d === "failed" ? d : { ...d, list: withRepository(d.list, repo) }));
  };

  const disconnect = () => {
    if (linking !== null) return;
    setLinking("disconnect");
    github.disconnect().then(
      () => {
        if (!live.current) return;
        setLinking(null);
        setAsk(null);
        toast({ title: SETTINGS_COPY.toastGitHubDisconnected, tone: "success" });
        load();
      },
      (e: unknown) => {
        if (!live.current) return;
        setLinking(null);
        react(githubFailureOf(e));
      },
    );
  };
  const remove = (repo: ProjectRepository) => {
    withBusy(repo.id, true);
    github.remove(projectId, repo.id).then(
      () => {
        if (!live.current) return;
        withBusy(repo.id, false);
        setAsk(null);
        setData((d) => (d === null || d === "failed" ? d : { ...d, list: withoutRepository(d.list, repo.id) }));
        toast({ title: SETTINGS_COPY.toastRepoRemoved, tone: "success" });
      },
      (e: unknown) => {
        if (!live.current) return;
        withBusy(repo.id, false);
        setAsk(null);
        react(githubFailureOf(e));
      },
    );
  };
  const cloneAgain = (repo: ProjectRepository) => {
    withBusy(repo.id, true);
    github.clone(projectId, repo.id).then(
      (fresh) => {
        if (!live.current) return;
        withBusy(repo.id, false);
        putRepository(fresh);
        toast({ title: SETTINGS_COPY.toastCloneStarted, tone: "success" });
      },
      (e: unknown) => {
        if (!live.current) return;
        withBusy(repo.id, false);
        react(githubFailureOf(e));
      },
    );
  };
  const add = (githubRepoId: number) => {
    if (picker?.adding !== null && picker?.adding !== undefined) return;
    setPicker((p) => (p === null ? null : { ...p, adding: githubRepoId }));
    github.add(projectId, githubRepoId).then(
      (repo) => {
        if (!live.current) return;
        setPicker((p) => (p === null ? null : { ...p, adding: null }));
        putRepository(repo);
        toast({ title: SETTINGS_COPY.toastRepoAdded, tone: "success" });
      },
      (e: unknown) => {
        if (!live.current) return;
        setPicker((p) => (p === null ? null : { ...p, adding: null }));
        react(githubFailureOf(e));
      },
    );
  };

  const owner = list.canManage;
  const row = owner ? githubRowOf(connection) : null;
  return (
    <>
      {owner ? (
        <GitHubAccount
          connection={connection}
          busy={linking !== null}
          onConnect={connect}
          onDisconnect={() => {
            setAsk({ kind: "disconnect" });
          }}
          onChoose={choose}
        />
      ) : null}
      <Repositories
        list={list}
        row={row}
        busyIds={busyIds}
        onAdd={() => {
          setPicker({ state: "loading", query: "", adding: null });
          loadPicker();
        }}
        onClone={cloneAgain}
        onRemove={(repo) => {
          setAsk({ kind: "remove", repo });
        }}
      />
      {picker === null ? null : (
        <AddDialog
          state={picker.state}
          query={picker.query}
          addedIds={new Set(list.repositories.map((r) => r.githubRepoId))}
          adding={picker.adding}
          choosing={linking === "choose"}
          onQuery={(q) => {
            setPicker((p) => (p === null ? null : { ...p, query: q }));
          }}
          onAdd={add}
          onRetry={loadPicker}
          onChoose={choose}
          onClose={() => {
            setPicker(null);
          }}
        />
      )}
      {ask?.kind === "disconnect" ? (
        <ConfirmDialog
          title={SETTINGS_COPY.githubDisconnectTitle}
          body={SETTINGS_COPY.githubDisconnectBody}
          confirmText={SETTINGS_COPY.githubDisconnect}
          testId="github-disconnect"
          busy={linking === "disconnect"}
          onConfirm={disconnect}
          onClose={() => {
            setAsk(null);
          }}
        />
      ) : null}
      {ask?.kind === "remove" ? (
        <ConfirmDialog
          title={SETTINGS_COPY.removeRepoTitle}
          body={`${ask.repo.fullName}: ${SETTINGS_COPY.removeRepoBody}`}
          confirmText={SETTINGS_COPY.removeRepo}
          testId="github-remove"
          busy={busyIds.has(ask.repo.id)}
          onConfirm={() => {
            remove(ask.repo);
          }}
          onClose={() => {
            setAsk(null);
          }}
        />
      ) : null}
    </>
  );
}
