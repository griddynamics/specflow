"use client";
// FR-GWEB-04, FR-GWEB-05 (docs/graph/gap/api.md "User interface"; answers.md "The question card", "Applying answers", "Review list",
// "Raising a gap by hand"): the Gaps screen, built from the decision components (DecisionList, DecisionRow, SeverityBadge, OptionCards with
// the "Recommended" mark) over the real gaps port.
//  - Gate (as Plan): `useReadyGate`; not Ready or a failed read -> `NotReadyState` "No gaps yet" + "Open Mission Control"; data and `?gap=`
//    load only once the gate is open.
//  - Header row: the Inbox / Answered by AI / Noise report / Documents tabs; on the inbox the filters (status, kind, severity, origin, story,
//    document) join it. The filters go to the server (`list_gaps`, which orders by severity, reach, then newest): the inbox asks for route
//    `ask` and `held_back=false` (its default view); the questions a run held back until the next run (FR-GRTE-06) are read with
//    `held_back=true` and listed under "More found in this run", collapsed. Above the list: "Apply now" (applies the answers waiting for the
//    quiet period; `409 gap_wrong_state` means none waits) and "Raise a gap" (a dialog; the new gap's card opens).
//  - `?gap=<id>` opens the question card (GapCard, answerable here), also when the filters hide the row; closing removes the parameter
//    (history.replaceState). After an answer, a withdrawal or a dismissal the lists read again without a loading state.
//  - "Answered by AI" (AiAnswers), the noise report with its rules (NoiseReport) and "Documents" (FR-GWEB-05, DocumentsTab) are tabs.
//  - `503 gap_unavailable` (gap runs not configured) shows "Gaps are not available", never an error page.
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
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
import { PageTitle } from "@/components/ui/TopBar";
import { usePorts, useRoutes } from "@/lib/data/provider";
import { isDataError } from "@/lib/domain/errors";
import type { Gap, GapDocument, GapsPort } from "@/lib/domain/gaps";
import { parseGapsQuery } from "@/lib/routes";
import { AiAnswers } from "./AiAnswers";
import { DocumentsTab } from "./DocumentsTab";
import { GapCard, type CardControl } from "./GapCard";
import { RaiseGapDialog } from "./GapDialogs";
import { GapsUnavailable } from "./GapsUnavailable";
import { NoiseReport } from "./NoiseReport";
import { gapLink } from "./gap-links";
import {
  DEFAULT_GAP_FILTERS,
  KIND_LABEL,
  KIND_OPTIONS,
  ORIGIN_LABEL,
  ORIGIN_OPTIONS,
  SEVERITY_LABEL,
  SEVERITY_LEVEL,
  SEVERITY_OPTIONS,
  STATUS_OPTIONS,
  countText,
  failureOf,
  failureText,
  gapQuery,
  inboxOrder,
  patchOf,
  rowSubtitle,
  withAll,
  type GapFilters,
} from "./gaps-logic";
import "@/components/shell/ready-gate.css";
import "./gaps.css";

type View = "inbox" | "ai" | "noise" | "documents";
const VIEWS: { value: View; label: string }[] = [
  { value: "inbox", label: "Inbox" },
  { value: "ai", label: "Answered by AI" },
  { value: "noise", label: "Noise report" },
  { value: "documents", label: "Documents" },
];

/** `key={projectId}`: another project starts with a fresh gate, filters and card. */
export function GapsScreen({ projectId }: Readonly<{ projectId: string }>) {
  return <Gaps key={projectId} projectId={projectId} />;
}

function GapsSkeleton() {
  return (
    <div className="gaps-skel" data-testid="gaps-loading" aria-busy="true">
      <Skeleton variant="block" height={44} />
      <Skeleton variant="block" height={280} />
    </div>
  );
}

/** The data layer of the screen: the ports it reads and the route builder. */
function useGapsDeps() {
  const { gaps, plan } = usePorts();
  return { gaps, plan, routes: useRoutes() };
}

function Gaps({ projectId }: Readonly<{ projectId: string }>) {
  const { routes } = useGapsDeps();
  const router = useRouter();
  const gate = useReadyGate(projectId);
  let body: ReactNode;
  if (gate === "checking") body = <GapsSkeleton />;
  else if (gate === "blocked") {
    body = (
      <NotReadyState
        testId="gaps-gate"
        icon="list-checks"
        title="No gaps yet"
        onOpenMissionControl={() => {
          router.push(routes.missionControl(projectId));
        }}
      >
        Gaps appear here after Specflow has processed this project&apos;s files.
      </NotReadyState>
    );
  } else body = <GapsContent projectId={projectId} />;
  return (
    <section className="gaps-screen" data-testid="screen-gaps" data-project={projectId}>
      <PageTitle className="shell-sr">Gaps</PageTitle>
      {body}
    </section>
  );
}

// ---- data ----

interface ListState {
  gaps: Gap[];
  next: string | null;
  phase: "loading" | "ready" | "failed" | "unavailable";
}
const LOADING: ListState = { gaps: [], next: null, phase: "loading" };

/**
 * One `list_gaps` view (the inbox, or the held-back questions): the first page on every filter change, then "Show more". `version` reads
 * the first page again after a write and keeps the shown rows until it answers (no loading state).
 */
function useGapList(gaps: GapsPort, projectId: string, filters: GapFilters, heldBack: boolean, tries: number, version: number) {
  const [state, setState] = useState<{ key: string; list: ListState }>({ key: "", list: LOADING });
  const key = `${JSON.stringify(filters)}|${String(tries)}`;
  useEffect(() => {
    let alive = true;
    gaps.list(projectId, gapQuery(filters, heldBack)).then(
      (page) => {
        if (alive) setState({ key, list: { gaps: page.gaps, next: page.next, phase: "ready" } });
      },
      (e: unknown) => {
        if (alive) setState({ key, list: { gaps: [], next: null, phase: failureOf(e) } });
      },
    );
    return () => {
      alive = false;
    };
  }, [gaps, projectId, filters, heldBack, key, version]);
  const list = state.key === key ? state.list : LOADING;
  const more = useCallback(() => {
    const cursor = list.next;
    if (cursor === null) return;
    gaps.list(projectId, { ...gapQuery(filters, heldBack), cursor }).then(
      (page) => {
        setState((s) => (s.key === key ? { key, list: { gaps: [...s.list.gaps, ...page.gaps], next: page.next, phase: "ready" } } : s));
      },
      () => undefined, // the button stays; the next click tries again
    );
  }, [gaps, projectId, filters, heldBack, key, list.next]);
  return { list, more };
}

/** The Story (from the plan) and Document filters' values; a failed read leaves that filter out. */
function useFilterSources(projectId: string) {
  const { gaps, plan } = useGapsDeps();
  const [stories, setStories] = useState<GapDocument[]>([]);
  const [documents, setDocuments] = useState<GapDocument[]>([]);
  useEffect(() => {
    let alive = true;
    plan.state(projectId).then(
      (s) => {
        if (alive && s.phase === "ready") setStories(s.plan.stories.map((st) => ({ id: st.storyId, name: st.title })));
      },
      () => undefined,
    );
    gaps.documents(projectId).then(
      (docs) => {
        if (alive) setDocuments(docs);
      },
      () => undefined,
    );
    return () => {
      alive = false;
      plan.leave(projectId); // no plan poll outlives the screen
    };
  }, [gaps, plan, projectId]);
  return { stories, documents };
}

/** The gap `?gap=` names, once loaded; an unknown id opens nothing and drops the parameter. `reload` reads it again. */
function useOpenGap(projectId: string, openId: string | null): { card: Gap | null; reload: () => Promise<void> } {
  const { gaps, routes } = useGapsDeps();
  const [fetched, setFetched] = useState<Gap | null>(null);
  const reload = useCallback(async () => {
    if (openId === null || openId === "") return;
    const g = await gaps.get(projectId, openId);
    setFetched(g);
  }, [gaps, projectId, openId]);
  useEffect(() => {
    if (openId === null || openId === "") return;
    let alive = true;
    gaps.get(projectId, openId).then(
      (g) => {
        if (alive) setFetched(g);
      },
      () => {
        if (alive) window.history.replaceState(null, "", routes.gaps(projectId));
      },
    );
    return () => {
      alive = false;
    };
  }, [gaps, projectId, openId, routes]);
  return { card: openId !== null && fetched?.id === openId ? fetched : null, reload };
}

// ---- view ----

function GapRows({ gaps, label, onOpen }: Readonly<{ gaps: Gap[]; label: string; onOpen: (id: string) => void }>) {
  return (
    <DecisionList label={label} columns={["Gap", "Kind", "Severity", "Origin"]}>
      {gaps.map((g) => (
        <DecisionRow
          key={g.id}
          title={g.title}
          subtitle={rowSubtitle(g)}
          domain={KIND_LABEL[g.kind]}
          severity={SEVERITY_LEVEL[g.severity]}
          severityLabel={SEVERITY_LABEL[g.severity]}
          status={g.status === "open" ? "open" : "answered"}
          who={<span>{ORIGIN_LABEL[g.origin]}</span>}
          onClick={() => {
            onOpen(g.id);
          }}
        />
      ))}
    </DecisionList>
  );
}

function ShowMore({ next, onMore }: Readonly<{ next: string | null; onMore: () => void }>) {
  if (next === null) return null;
  return (
    <div className="gaps-more">
      <Button variant="glass" size="sm" onClick={onMore}>
        Show more
      </Button>
    </div>
  );
}

function Filters({
  filters,
  stories,
  documents,
  onChange,
}: Readonly<{
  filters: GapFilters;
  stories: GapDocument[];
  documents: GapDocument[];
  onChange: (patch: Partial<GapFilters>) => void;
}>) {
  const select = (key: keyof GapFilters, label: string, options: { value: string; label: string }[]) => (
    <Select
      label={label}
      options={options}
      value={filters[key]}
      onChange={(v) => {
        onChange(patchOf(key, v));
      }}
    />
  );
  return (
    <div className="gdg-filters" data-testid="gap-filters">
      {select("status", "Status", STATUS_OPTIONS)}
      {select("kind", "Kind", KIND_OPTIONS)}
      {select("severity", "Severity", SEVERITY_OPTIONS)}
      {select("origin", "Origin", ORIGIN_OPTIONS)}
      {stories.length === 0 ? null : select("story", "Story", withAll(stories))}
      {documents.length === 0 ? null : select("document", "Document", withAll(documents))}
    </div>
  );
}

/** "More found in this run" (FR-GRTE-06): the questions the run held back from the default view, collapsed until asked for. */
function HeldBack({
  gaps,
  next,
  onMore,
  onOpen,
}: Readonly<{ gaps: Gap[]; next: string | null; onMore: () => void; onOpen: (id: string) => void }>) {
  const [open, setOpen] = useState(false);
  if (gaps.length === 0) return null;
  return (
    <section className="gap-sec gaps-held" aria-label="More found in this run" data-testid="gaps-held-back">
      <div className="gaps-held__head">
        <h2 className="gap-sec__title eyebrow">More found in this run</h2>
        <Button
          variant="plain"
          size="sm"
          aria-expanded={open}
          onClick={() => {
            setOpen((o) => !o);
          }}
        >
          {open ? "Hide" : `Show ${countText(gaps.length, next !== null)}`}
        </Button>
      </div>
      <p className="gaps-count body-sm">These questions are shown with the next gap run.</p>
      {open ? (
        <>
          <GapRows gaps={gaps} label="More found in this run" onOpen={onOpen} />
          <ShowMore next={next} onMore={onMore} />
        </>
      ) : null}
    </section>
  );
}

/** "Apply now" and "Raise a gap" above the inbox, with the outcome of "Apply now" in words. */
function InboxActions({ projectId, onApplied, onRaise }: Readonly<{ projectId: string; onApplied: () => void; onRaise: () => void }>) {
  const { gaps } = useGapsDeps();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const apply = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const r = await gaps.applyNow(projectId);
      const n = r.clrIds.length;
      const what = n === 1 ? "1 answer is" : `${String(n)} answers are`;
      const build = r.build === null ? "" : ` (build ${r.build})`;
      setMessage(`${what} being applied${build}. The gaps resolve when the build finishes.`);
      onApplied();
    } catch (e) {
      setMessage(
        isDataErrorCode(e, "gap_wrong_state") ? "No answers are waiting to be applied." : `Answers could not be applied. ${failureText(e)}`,
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="gaps-actions" data-testid="gaps-actions">
      <p className="gaps-actions__msg body-sm" role="status" data-testid="gaps-apply-result">
        {message ?? ""}
      </p>
      <Button variant="glass" size="sm" onClick={() => void apply()} disabled={busy} data-testid="gaps-apply-now">
        Apply now
      </Button>
      <Button variant="glass" size="sm" onClick={onRaise} data-testid="gaps-raise">
        Raise a gap
      </Button>
    </div>
  );
}

const isDataErrorCode = (e: unknown, apiCode: string): boolean => isDataError(e) && e.detail?.apiCode === apiCode;

function Inbox({
  inbox,
  held,
  filtered,
  onOpen,
  onRetry,
}: Readonly<{
  inbox: ReturnType<typeof useGapList>;
  held: ReturnType<typeof useGapList>;
  filtered: boolean;
  onOpen: (id: string) => void;
  onRetry: () => void;
}>) {
  const { list } = inbox;
  if (list.phase === "loading") return <GapsSkeleton />;
  if (list.phase === "unavailable") return <GapsUnavailable />;
  if (list.phase === "failed") {
    return (
      <EmptyState title="Gaps could not be loaded" icon="warning-circle" actions={<Button onClick={onRetry}>Try again</Button>}>
        Check your connection and try again.
      </EmptyState>
    );
  }
  const shown = inboxOrder(list.gaps);
  let rows: ReactNode;
  if (shown.length > 0) rows = <GapRows gaps={shown} label="Gaps" onOpen={onOpen} />;
  else if (filtered) {
    rows = (
      <div data-testid="gaps-empty">
        <EmptyState title="No gaps match these filters" icon="funnel-simple">
          Try another status, kind or severity.
        </EmptyState>
      </div>
    );
  } else {
    rows = (
      <div data-testid="gaps-none">
        <EmptyState title="No open gaps" icon="check-circle">
          Gaps appear here when a gap run finds something the specification leaves open.
        </EmptyState>
      </div>
    );
  }
  return (
    <>
      {rows}
      <ShowMore next={list.next} onMore={inbox.more} />
      <p className="gaps-count body-sm" role="status" data-testid="gap-count">
        {shown.length === 0 ? "" : countText(shown.length, list.next !== null)}
      </p>
      {held.list.phase === "ready" ? <HeldBack gaps={held.list.gaps} next={held.list.next} onMore={held.more} onOpen={onOpen} /> : null}
    </>
  );
}

function GapsContent({ projectId }: Readonly<{ projectId: string }>) {
  const { gaps, routes } = useGapsDeps();
  const openId = parseGapsQuery(useSearchParams()).gapId;
  const [view, setView] = useState<View>("inbox");
  const [filters, setFilters] = useState<GapFilters>(DEFAULT_GAP_FILTERS);
  const [tries, setTries] = useState(0);
  const [version, setVersion] = useState(0);
  const [raising, setRaising] = useState(false);
  const inbox = useGapList(gaps, projectId, filters, false, tries, version);
  const held = useGapList(gaps, projectId, filters, true, tries, version);
  const { stories, documents } = useFilterSources(projectId);
  const { card, reload } = useOpenGap(projectId, openId);
  const filtered = useMemo(() => JSON.stringify(filters) !== JSON.stringify(DEFAULT_GAP_FILTERS), [filters]);

  const href = useCallback((id: string) => gapLink(routes, projectId, id), [routes, projectId]);
  const open = useCallback(
    (id: string) => {
      window.history.replaceState(null, "", gapLink(routes, projectId, id));
    },
    [routes, projectId],
  );
  const close = useCallback(() => {
    window.history.replaceState(null, "", routes.gaps(projectId));
  }, [routes, projectId]);
  const changed = useCallback(() => {
    setVersion((n) => n + 1);
  }, []);
  const control = useMemo<CardControl>(
    () => ({
      gaps,
      projectId,
      href,
      onOpenGap: open,
      onReload: reload,
      onChanged: changed,
      onDismissed: () => {
        close();
        changed();
      },
    }),
    [gaps, projectId, href, open, reload, changed, close],
  );

  let content: ReactNode;
  if (view === "inbox") {
    content = (
      <>
        <InboxActions
          projectId={projectId}
          onApplied={changed}
          onRaise={() => {
            setRaising(true);
          }}
        />
        <Inbox
          inbox={inbox}
          held={held}
          filtered={filtered}
          onOpen={open}
          onRetry={() => {
            setTries((n) => n + 1);
          }}
        />
      </>
    );
  } else if (view === "ai") {
    content = <AiAnswers gaps={gaps} projectId={projectId} href={href} onOpenGap={open} onChanged={changed} />;
  } else if (view === "noise") {
    content = <NoiseReport gaps={gaps} projectId={projectId} />;
  } else {
    content = <DocumentsTab gaps={gaps} projectId={projectId} href={href} onOpenGap={open} version={version} />;
  }

  return (
    <>
      <ShellHeader
        tabs={
          <SegmentedControl
            label="View"
            options={VIEWS}
            value={view}
            testId="gaps-tab"
            onChange={(v) => {
              setView(VIEWS.find((x) => x.value === v)?.value ?? "inbox");
            }}
          />
        }
        tools={
          view === "inbox" ? (
            <Filters
              filters={filters}
              stories={stories}
              documents={documents}
              onChange={(patch) => {
                setFilters((f) => ({ ...f, ...patch }));
              }}
            />
          ) : undefined
        }
      />
      {content}
      {card ? <GapCard key={card.id} gap={card} onClose={close} control={control} /> : null}
      {raising ? (
        <RaiseGapDialog
          gaps={gaps}
          projectId={projectId}
          onClose={() => {
            setRaising(false);
          }}
          onRaised={(gapId) => {
            setRaising(false);
            changed();
            open(gapId);
          }}
        />
      ) : null}
    </>
  );
}
