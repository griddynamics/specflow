"use client";
// docs/jira-sync/ui.md#jira-tab: the Jira tab. The owner types the Jira project and Epic keys, checks access, saves, and turns the sync on
// or off; a collaborator sees the same blocks read-only. The rules are in jira-logic.ts, the pieces in jira-parts.tsx. The sync itself has
// no button here: it runs after plan runs and through MCP.
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useReducer, useRef } from "react";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { JiraView } from "./jira-parts";
import { failureOf, INITIAL_TAB, isRunning, phaseOf, startPolling, tabReducer, targetOf, type Busy, type Failure } from "./jira-logic";
import { LoadFailed } from "./parts";
import { SETTINGS_COPY } from "./settings-copy";
import { useJiraPort, useSettingsRoutes } from "./use-settings-ports";

export function JiraTab({ projectId, projectName }: Readonly<{ projectId: string; projectName: string | null }>) {
  const jira = useJiraPort();
  const routes = useSettingsRoutes();
  const router = useRouter();
  const toast = useToast();
  const [st, dispatch] = useReducer(tabReducer, INITIAL_TAB);
  const live = useRef(true);
  const run = useRef(0); // the token of the latest edit or call (see tabReducer)
  const writes = useRef(0); // saves and switches answered: a slower, older load must not overwrite them
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const goHome = useCallback(() => {
    router.replace(routes.home());
  }, [router, routes]);

  const load = useCallback(() => {
    const seen = writes.current;
    jira.get(projectId).then(
      (settings) => {
        if (live.current && seen === writes.current) dispatch({ type: "loaded", settings });
      },
      (e: unknown) => {
        if (!live.current) return;
        if (failureOf(e).kind === "gone") goHome();
        else dispatch({ type: "load_failed" });
      },
    );
  }, [jira, projectId, goHome]);
  useEffect(() => {
    load();
  }, [load]);

  const running = isRunning(st.settings);
  useEffect(() => startPolling(running, load), [running, load]);

  const { settings: state } = st;
  if (state === null) {
    return (
      <div aria-hidden data-testid="settings-loading">
        <Skeleton variant="block" height={200} />
      </div>
    );
  }
  if (state === "failed") {
    return (
      <LoadFailed
        text={SETTINGS_COPY.jiraLoadFailed}
        onRetry={() => {
          dispatch({ type: "retry" });
          load();
        }}
      />
    );
  }

  const react = (f: Failure) => {
    if (f.kind === "gone") goHome();
    else if (f.kind === "toast") toast({ title: f.text, tone: "danger" });
  };
  const edit = (field: "project" | "epic") => (value: string) => {
    dispatch({ type: "edit", field, value, run: ++run.current });
  };
  const call = <T,>(kind: Busy, request: Promise<T>, done: (r: T, mine: number) => void) => {
    const mine = ++run.current;
    dispatch({ type: "start", kind, run: mine });
    request.then(
      (r) => {
        if (live.current) done(r, mine);
      },
      (e: unknown) => {
        if (!live.current) return;
        const failure = failureOf(e);
        dispatch({ type: "failed", kind, run: mine, failure });
        react(failure);
      },
    );
  };

  const primary = () => {
    const target = targetOf(st.project, st.epic);
    if (st.outcome?.kind === "ok") {
      call("save", jira.save(projectId, { ...target, enabled: state.enabled }), (settings, mine) => {
        writes.current += 1;
        dispatch({ type: "saved", run: mine, settings, target });
        toast({ title: SETTINGS_COPY.toastJiraSaved, tone: "success" });
      });
    } else {
      call("check", jira.checkAccess(projectId, target), (access, mine) => {
        dispatch({ type: "checked", run: mine, access });
      });
    }
  };
  const toggle = (enabled: boolean) => {
    if (state.jiraProject === null || st.busy === "switch") return;
    call("switch", jira.save(projectId, { jiraProject: state.jiraProject, epicKey: state.epicKey, enabled }), (settings) => {
      writes.current += 1;
      dispatch({ type: "switched", settings });
    });
  };

  return (
    <JiraView
      settings={state}
      projectName={projectName}
      project={st.project}
      epic={st.epic}
      phase={phaseOf(st.outcome, st.busy === "switch" ? null : st.busy)}
      outcome={st.outcome}
      switching={st.busy === "switch"}
      onProject={edit("project")}
      onEpic={edit("epic")}
      onPrimary={primary}
      onSwitch={toggle}
    />
  );
}
