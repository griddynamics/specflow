"use client";
// FE-DEC-01..04, FE-DEC-02.AC5, FE-BP-03.AC8, D19, FE-DS-08.AC3 (PLAN a.8, b.6; TECH-SPECS §1.5, §2.10): the Decisions screen. Keep the path, the export
// name, the `projectId` prop and the `screen-decisions` hook of the screen (P07 page binding).
//  - Gate (FE-DEC-02.AC5, D19): `useReadyGate`; not Ready or a failed read → `NotReadyState` "No decisions yet" + "Open Mission Control", no
//    ShellHeader, no modal; data and `?decision=` load only once the gate is open. `decisions-none` = a Ready project with no decisions.
//  - Filters, in the shell's header row (ShellHeader): Severity (All) is the tab bar; the filter row (`.gdg-filters`: "Open" on,
//    "Assigned to me" off, Domain All) joins it on wide screens and wraps under it otherwise. The list is the pure `filterDecisions`
//    over `decisions.list`, with the active user from `people.me()` (never a literal name). The h1 is visually hidden: the sidebar
//    names the page.
//  - The URL is the state of the modal: `?decision=<id>` (decisionLink) opens it, also when the filters hide the row; closing
//    removes the parameter (history.replaceState, which Next.js syncs with useSearchParams: no server round trip, as in P09).
//  - Submit calls `decisions.submit` (records nothing), shows the toast, closes the modal; the list is not reloaded or changed.
//  - A source link closes the modal and opens the Files deep link at the section (router.push(docRefLink), P11 contract).
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { NotReadyState } from "@/components/shell/NotReadyState";
import { ShellHeader } from "@/components/shell/ShellHeader";
import { useReadyGate } from "@/components/shell/use-ready-gate";
import { Button } from "@/components/ui/Button";
import { DecisionList } from "@/components/ui/DecisionList";
import { DecisionRow } from "@/components/ui/DecisionRow";
import { EmptyState } from "@/components/ui/EmptyState";
import { Select } from "@/components/ui/Select";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Skeleton } from "@/components/ui/Skeleton";
import { Switch } from "@/components/ui/Switch";
import { useToast } from "@/components/ui/Toast";
import { usePorts, useRoutes } from "@/lib/data/provider";
import type { DocRef } from "@/lib/domain/common";
import { DEFAULT_DECISION_FILTERS, type Decision, type DecisionAnswer, type DecisionFilters } from "@/lib/domain/decisions";
import { filterDecisions } from "@/lib/domain/logic/filter-decisions";
import { parseDecisionsQuery } from "@/lib/routes";
import { docRefLink } from "@/lib/markdown/links";
import { DecisionModal } from "./DecisionModal";
import { decisionLink } from "./decision-links";
import { SEVERITY_OPTIONS, countText, domainOptions, submitToastTitle, toSeverityFilter } from "./decisions-logic";
import "@/components/shell/ready-gate.css";
import "./dec-gaps.css";
import { PageTitle } from "@/components/ui/TopBar";

interface Loaded {
  list: Decision[];
  domains: string[];
  meId: string;
  names: Record<string, string>;
}

type LoadState = Loaded | "failed" | null;

/** The data layer of the screen: the ports it reads and the route builder. */
function useDecisionsDeps() {
  const { decisions, people } = usePorts();
  return { decisions, people, routes: useRoutes() };
}

/** The list once it has loaded; null while loading or after a failed read (a deep link can submit before the list has loaded). */
function loadedOf(data: LoadState): Loaded | null {
  return data !== null && data !== "failed" ? data : null;
}

/** `key={projectId}`: another project starts with a fresh gate, filters and modal. */
export function DecisionsScreen({ projectId }: Readonly<{ projectId: string }>) {
  return <Decisions key={projectId} projectId={projectId} />;
}

function Decisions({ projectId }: Readonly<{ projectId: string }>) {
  const { routes } = useDecisionsDeps();
  const router = useRouter();
  const gate = useReadyGate(projectId);
  let body: ReactNode;
  if (gate === "checking") {
    body = (
      <div className="dec-skel" data-testid="decisions-loading" aria-busy="true">
        <Skeleton variant="block" height={44} />
        <Skeleton variant="block" height={280} />
      </div>
    );
  } else if (gate === "blocked") {
    body = (
      <NotReadyState
        testId="decisions-gate"
        icon="list-checks"
        title="No decisions yet"
        onOpenMissionControl={() => {
          router.push(routes.missionControl(projectId));
        }}
      >
        Decisions appear here after Specflow has processed this project&apos;s files.
      </NotReadyState>
    );
  } else {
    body = <DecisionsContent projectId={projectId} />;
  }
  return (
    <section className="dec-screen" data-testid="screen-decisions" data-project={projectId}>
      <PageTitle className="shell-sr">Decisions</PageTitle>
      {body}
    </section>
  );
}

function DecisionsSkeleton() {
  return (
    <div className="dec-skel" data-testid="decisions-loading" aria-busy="true">
      <Skeleton variant="block" height={44} />
      <Skeleton variant="block" height={280} />
    </div>
  );
}

function DecisionsFailed({ onRetry }: Readonly<{ onRetry: () => void }>) {
  return (
    <EmptyState title="Decisions could not be loaded" icon="warning-circle" actions={<Button onClick={onRetry}>Try again</Button>}>
      Check your connection and try again.
    </EmptyState>
  );
}

function questionsSubtitle(d: Decision): string {
  const count = d.questions.length;
  return `${d.status === "answered" ? "Answered · " : ""}${String(count)} ${count === 1 ? "question" : "questions"}`;
}

function DecisionRows({ data, shown, onOpen }: Readonly<{ data: Loaded; shown: Decision[]; onOpen: (id: string) => void }>) {
  if (data.list.length === 0) {
    return (
      <div data-testid="decisions-none">
        <EmptyState title="No decisions yet" icon="list-checks">
          Decisions appear here after Specflow has processed this project&apos;s files.
        </EmptyState>
      </div>
    );
  }
  if (shown.length === 0) {
    return (
      <div data-testid="decisions-empty">
        <EmptyState title="No decisions match these filters" icon="funnel-simple">
          Try turning off &quot;Open&quot; or choosing another domain.
        </EmptyState>
      </div>
    );
  }
  return (
    <DecisionList>
      {shown.map((d) => (
        <DecisionRow
          key={d.id}
          title={d.title}
          subtitle={questionsSubtitle(d)}
          domain={d.domain}
          severity={d.severity}
          assignee={data.names[d.assigneeId]}
          status={d.status}
          onClick={() => {
            onOpen(d.id);
          }}
        />
      ))}
    </DecisionList>
  );
}

function DecisionsLoaded({
  data,
  filters,
  shown,
  onFilters,
  onOpen,
}: Readonly<{
  data: Loaded;
  filters: DecisionFilters;
  shown: Decision[];
  onFilters: (patch: Partial<DecisionFilters>) => void;
  onOpen: (id: string) => void;
}>) {
  return (
    <>
      <ShellHeader
        tabs={
          <SegmentedControl
            label="Severity"
            options={SEVERITY_OPTIONS}
            value={filters.severity}
            onChange={(v) => {
              onFilters({ severity: toSeverityFilter(v) });
            }}
          />
        }
        tools={
          <div className="gdg-filters" data-testid="decision-filters">
            <Switch
              label="Open"
              checked={filters.openOnly}
              onChange={(v) => {
                onFilters({ openOnly: v });
              }}
            />
            <Switch
              label="Assigned to me"
              checked={filters.assignedToMe}
              onChange={(v) => {
                onFilters({ assignedToMe: v });
              }}
            />
            <Select
              label="Domain"
              icon="funnel-simple"
              options={domainOptions(data.domains)}
              value={filters.domain}
              onChange={(v) => {
                onFilters({ domain: v });
              }}
            />
          </div>
        }
      />
      <DecisionRows data={data} shown={shown} onOpen={onOpen} />
      <p className="dec-count body-sm" role="status" data-testid="decision-count">
        {data.list.length === 0 ? "" : countText(shown.length, data.list.length)}
      </p>
    </>
  );
}

/** Loads the list, the domains and the names of the people; `tries` re-runs the load. */
function useLoadedDecisions(
  deps: Pick<ReturnType<typeof useDecisionsDeps>, "decisions" | "people">,
  projectId: string,
  tries: number,
): LoadState {
  const { decisions, people } = deps;
  const [data, setData] = useState<LoadState>(null);
  useEffect(() => {
    let alive = true;
    Promise.all([decisions.list(projectId), decisions.domains(projectId), people.me(), people.directory()]).then(
      ([list, domains, me, dir]) => {
        if (!alive) return;
        const names: Record<string, string> = {};
        for (const p of [...dir, me]) names[p.id] = p.name;
        setData({ list, domains, meId: me.id, names });
      },
      () => {
        if (alive) setData("failed");
      },
    );
    return () => {
      alive = false;
    };
  }, [decisions, people, projectId, tries]);
  return data;
}

/** The decision `?decision=` last loaded. An unknown id opens nothing and drops the parameter. */
function useFetchedDecision(
  deps: Pick<ReturnType<typeof useDecisionsDeps>, "decisions" | "routes">,
  projectId: string,
  openId: string | null,
): Decision | null {
  const { decisions, routes } = deps;
  const [fetched, setFetched] = useState<Decision | null>(null);
  useEffect(() => {
    if (openId === null || openId === "") return;
    let alive = true;
    decisions.get(projectId, openId).then(
      (d) => {
        if (alive) setFetched(d);
      },
      () => {
        if (alive) window.history.replaceState(null, "", routes.decisions(projectId));
      },
    );
    return () => {
      alive = false;
    };
  }, [decisions, projectId, openId, routes]);
  return fetched;
}

function DecisionsContent({ projectId }: Readonly<{ projectId: string }>) {
  const deps = useDecisionsDeps();
  const { decisions, routes } = deps;
  const router = useRouter();
  const toast = useToast();
  const query = useSearchParams();
  const openId = parseDecisionsQuery(query).decisionId;

  const [tries, setTries] = useState(0);
  const [filters, setFilters] = useState<DecisionFilters>(DEFAULT_DECISION_FILTERS);
  const data = useLoadedDecisions(deps, projectId, tries);
  const fetched = useFetchedDecision(deps, projectId, openId);
  const modal = openId !== null && openId !== "" && fetched?.id === openId ? fetched : null;
  const [submitting, setSubmitting] = useState(false);
  const submitLock = useRef(false);

  const closeModal = useCallback(() => {
    window.history.replaceState(null, "", routes.decisions(projectId));
  }, [routes, projectId]);

  const ready = loadedOf(data);
  const shown = useMemo(() => (ready === null ? [] : filterDecisions(ready.list, filters, ready.meId)), [ready, filters]);

  function open(id: string) {
    window.history.replaceState(null, "", decisionLink(routes, projectId, id));
  }

  async function submit(answer: DecisionAnswer) {
    if (submitLock.current) return;
    submitLock.current = true;
    setSubmitting(true);
    try {
      const { assigneeId } = await decisions.submit(projectId, answer);
      const loaded = ready; // a deep link can submit before the list has loaded
      toast({
        title: loaded === null ? "Answer submitted." : submitToastTitle(assigneeId, loaded.meId, (id) => loaded.names[id]),
        tone: "success",
      });
      closeModal();
    } catch {
      toast({ title: "Could not submit the answer", message: "Try again.", tone: "danger" });
    } finally {
      submitLock.current = false;
      setSubmitting(false);
    }
  }

  // While a submit is in flight the modal stays open (Esc, Cancel, scrim and the close button are ignored), so the toast never
  // arrives after the modal has gone.
  const guardedClose = useCallback(() => {
    if (!submitLock.current) closeModal();
  }, [closeModal]);

  function openDoc(ref: DocRef) {
    window.history.replaceState(null, "", routes.decisions(projectId)); // the Decisions entry keeps no ?decision=, so Back shows the list
    router.push(docRefLink(routes, projectId, ref));
  }

  const set = (patch: Partial<DecisionFilters>) => {
    setFilters((f) => ({ ...f, ...patch }));
  };

  let body: ReactNode;
  if (data === null) {
    body = <DecisionsSkeleton />;
  } else if (data === "failed") {
    body = (
      <DecisionsFailed
        onRetry={() => {
          setTries((n) => n + 1);
        }}
      />
    );
  } else {
    body = <DecisionsLoaded data={data} filters={filters} shown={shown} onFilters={set} onOpen={open} />;
  }

  return (
    <>
      {body}
      {modal ? (
        <DecisionModal
          key={modal.id}
          decision={modal}
          assigneeName={ready?.names[modal.assigneeId]}
          submitting={submitting}
          onClose={guardedClose}
          onSubmit={(answer) => {
            void submit(answer);
          }}
          onOpenDoc={openDoc}
        />
      ) : null}
    </>
  );
}
