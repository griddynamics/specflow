"use client";
// FR-GWEB-04, FR-GWEB-06 (docs/graph/gap/api.md "User interface"; answers.md "The question card", "Applying answers", "Review list",
// "Raising a gap by hand", "Who answers"): the Gaps screen, built from the decision components (DecisionList, DecisionRow, SeverityBadge,
// OptionCards with the "Recommended" mark) over the real gaps port. People list gaps, open one, assign it, and answer it: an answer is
// staged; "Resolve gaps" applies every staged answer of the project in one rebuild.
//  - Gate (as Plan): `useReadyGate`; not Ready or a failed read -> `NotReadyState` "No gaps yet" + "Open Mission Control"; data and `?gap=`
//    load only once the gate is open.
//  - Header row: the Inbox / Answered by AI / Noise report tabs, then "Raise a gap" (glass; a dialog, the new gap's card opens) and
//    "Resolve gaps" (the yellow action, ResolveGaps.tsx) on every tab; on the inbox the filters (status, kind, severity, story,
//    assigned) join it. The statuses read Open, Staged (stored `answered`, and `applying` with the "Resolving…" tag while its rebuild
//    runs) and Resolved; the Status filter offers Open (the default), Staged, Resolved and All. The table's last column is the assignee
//    (avatar and name, or "Unassigned"), changed in place: a click opens a menu of Unassigned and the members (AssigneeMenu); a failed
//    assignment is said above the list. The Assigned filter offers All (the default), Unassigned and each member (the members port).
//    The filters go to the server (`list_gaps`, which orders by severity, reach, then newest): the inbox asks for route `ask` and lists
//    every gap a run found (FR-GRTE-06), a page at a time ("Show more" while more pages exist). While a gap run runs, each time the
//    check line shows it recorded another site or moved to its next step, the inbox reads its loaded range again (use-gap-inbox.ts), so
//    the run's gaps appear in place without a reload; the rows, pages, filters, open card, focus and scroll stay (use-keep-place.ts).
//    The gaps the latest run found carry "New" (row and card) until the next run starts.
//  - `?gap=<id>` opens the question card (GapCard, answerable here), also when the filters hide the row; closing removes the
//    parameter (history.replaceState). After an answer, an assignment, an unstage, a withdrawal, a dismissal or "Resolve gaps" the lists
//    read again without a loading state.
//  - "Answered by AI" (AiAnswers) and the noise report with its rules (NoiseReport) are tabs.
//  - Above every tab: the gap check status line (GapCheckStatus): whether the build or the gap run after it runs now, with the step,
//    progress and the New rows listed so far, or when the last check finished and what it found; when a run ends the lists read again.
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
import { Tag } from "@/components/ui/Tag";
import { PageTitle } from "@/components/ui/TopBar";
import { usePorts, useRoutes } from "@/lib/data/provider";
import type { Gap, GapCheck } from "@/lib/domain/gaps";
import type { Member } from "@/lib/domain/members";
import { parseGapsQuery } from "@/lib/routes";
import { AiAnswers } from "./AiAnswers";
import { AssigneeMenu, type AssignControl } from "./AssigneeMenu";
import { GapCheckStatus, useGapCheck } from "./GapCheckStatus";
import { GapCard, type CardControl } from "./GapCard";
import { RaiseGapDialog } from "./GapDialogs";
import { GapsUnavailable } from "./GapsUnavailable";
import { NoiseReport } from "./NoiseReport";
import { ResolveButton, ResolveDialog, useStaged } from "./ResolveGaps";
import { gapLink } from "./gap-links";
import {
  DEFAULT_GAP_FILTERS,
  KIND_LABEL,
  KIND_OPTIONS,
  NEW_LABEL,
  RESOLVING_LABEL,
  SEVERITY_LABEL,
  SEVERITY_LEVEL,
  SEVERITY_OPTIONS,
  STATUS_OPTIONS,
  assignedOptions,
  assigneeText,
  checkPulse,
  countText,
  failureText,
  inboxOrder,
  isNewGap,
  memberNames,
  patchOf,
  rowSubtitle,
  withAll,
  type GapFilters,
} from "./gaps-logic";
import { useGapInbox } from "./use-gap-inbox";
import { useKeepPlace } from "./use-keep-place";
import "@/components/shell/ready-gate.css";
import "./gaps.css";

type View = "inbox" | "ai" | "noise";
const VIEWS: { value: View; label: string }[] = [
  { value: "inbox", label: "Inbox" },
  { value: "ai", label: "Answered by AI" },
  { value: "noise", label: "Noise report" },
];

/** A Story filter value: the story id and its title. */
interface Story {
  id: string;
  name: string;
}

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
  const { gaps, plan, members } = usePorts();
  return { gaps, plan, members, routes: useRoutes() };
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

/** The Story filter's values (from the plan) and the project's members (the Assigned filter, the column and its menu); a failed read
 * leaves the Story filter out, or names no member (the filter and the menu keep Unassigned). */
function useFilterSources(projectId: string) {
  const { plan, members } = useGapsDeps();
  const [stories, setStories] = useState<Story[]>([]);
  const [people, setPeople] = useState<Member[]>([]);
  useEffect(() => {
    let alive = true;
    plan.state(projectId).then(
      (s) => {
        if (alive && s.phase === "ready") setStories(s.plan.stories.map((st) => ({ id: st.storyId, name: st.title })));
      },
      () => undefined,
    );
    members.list(projectId).then(
      (state) => {
        if (alive) setPeople(state.members);
      },
      () => undefined,
    );
    return () => {
      alive = false;
      plan.leave(projectId); // no plan poll outlives the screen
    };
  }, [plan, members, projectId]);
  return { stories, people };
}

/** The gap `?gap=` names, once loaded; an unknown id opens nothing and drops the parameter. `reload` reads it again. The id may be an
 * alias the API resolves (a migrated finding's old `FND::` id), so the card is matched by the id asked for, not by the gap's own id. */
function useOpenGap(projectId: string, openId: string | null): { card: Gap | null; reload: () => Promise<void> } {
  const { gaps, routes } = useGapsDeps();
  const [fetched, setFetched] = useState<{ asked: string; gap: Gap } | null>(null);
  const reload = useCallback(async () => {
    if (openId === null || openId === "") return;
    const g = await gaps.get(projectId, openId);
    setFetched({ asked: openId, gap: g });
  }, [gaps, projectId, openId]);
  useEffect(() => {
    if (openId === null || openId === "") return;
    let alive = true;
    gaps.get(projectId, openId).then(
      (g) => {
        if (alive) setFetched({ asked: openId, gap: g });
      },
      () => {
        if (alive) window.history.replaceState(null, "", routes.gaps(projectId));
      },
    );
    return () => {
      alive = false;
    };
  }, [gaps, projectId, openId, routes]);
  return { card: openId !== null && fetched?.asked === openId ? fetched.gap : null, reload };
}

// ---- view ----

/** The tags on a row's title line: "New" for a gap the latest run found, "Resolving…" while its rebuild runs. */
function rowBadge(g: Gap, isNew: boolean): ReactNode {
  if (!isNew && g.status !== "applying") return undefined;
  return (
    <>
      {isNew ? (
        <span data-testid="gap-row-new">
          <Tag tone="info">{NEW_LABEL}</Tag>
        </span>
      ) : null}
      {g.status === "applying" ? (
        <span data-testid="gap-row-resolving">
          <Tag tone="pending">{RESOLVING_LABEL}</Tag>
        </span>
      ) : null}
    </>
  );
}

function GapRows({
  gaps,
  check,
  assign,
  onOpen,
}: Readonly<{ gaps: Gap[]; check: GapCheck | null; assign: AssignControl; onOpen: (id: string) => void }>) {
  return (
    <DecisionList label="Gaps" columns={["Gap", "Kind", "Severity", "Assigned"]}>
      {gaps.map((g) => (
        <DecisionRow
          key={g.id}
          title={g.title}
          subtitle={rowSubtitle(g)}
          domain={KIND_LABEL[g.kind]}
          severity={SEVERITY_LEVEL[g.severity]}
          severityLabel={SEVERITY_LABEL[g.severity]}
          status={g.status === "open" ? "open" : "answered"}
          badge={rowBadge(g, isNewGap(g, check))}
          assignee={g.assignedTo === null ? undefined : assigneeText(g.assignedTo, assign.people)}
          aside={<AssigneeMenu gap={g} control={assign} />}
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
  people,
  onChange,
}: Readonly<{
  filters: GapFilters;
  stories: Story[];
  people: Member[];
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
      {stories.length === 0 ? null : select("story", "Story", withAll(stories))}
      {select("assigned", "Assigned", assignedOptions(people))}
    </div>
  );
}

function Inbox({
  inbox,
  check,
  assign,
  assignError,
  filtered,
  onOpen,
  onRetry,
}: Readonly<{
  inbox: ReturnType<typeof useGapInbox>;
  check: GapCheck | null;
  assign: AssignControl;
  assignError: string | null;
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
  if (shown.length > 0) rows = <GapRows gaps={shown} check={check} assign={assign} onOpen={onOpen} />;
  else if (filtered) {
    rows = (
      <div data-testid="gaps-empty">
        <EmptyState title="No gaps match these filters" icon="funnel-simple">
          Try another status, kind, severity or assignee.
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
      {assignError === null ? null : (
        <p className="gaps-count body-sm" role="alert" data-testid="gaps-assign-error">
          {assignError}
        </p>
      )}
      {rows}
      <ShowMore next={list.next} onMore={inbox.more} />
      <p className="gaps-count body-sm" role="status" data-testid="gap-count">
        {shown.length === 0 ? "" : countText(shown.length, list.next !== null)}
      </p>
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
  const [resolving, setResolving] = useState(false);
  const { stories, people } = useFilterSources(projectId);
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
  const check = useGapCheck(gaps, projectId, changed);
  const inbox = useGapInbox(gaps, projectId, filters, tries, version, checkPulse(check));
  const body = useKeepPlace();
  const staged = useStaged(gaps, projectId, version, changed);
  const [assignError, setAssignError] = useState<string | null>(null);
  const assign = useMemo<AssignControl>(
    () => ({
      people,
      assign: async (gap, userId) => {
        setAssignError(null);
        try {
          await gaps.assign(projectId, gap.id, userId);
        } catch (e) {
          setAssignError(`The assignment of "${gap.title}" was not saved. ${failureText(e)}`);
          throw e;
        }
        changed();
      },
    }),
    [gaps, projectId, people, changed],
  );
  const control = useMemo<CardControl>(
    () => ({
      gaps,
      projectId,
      members: people,
      href,
      onOpenGap: open,
      onReload: reload,
      onChanged: changed,
      onDismissed: () => {
        close();
        changed();
      },
    }),
    [gaps, projectId, people, href, open, reload, changed, close],
  );

  let content: ReactNode;
  if (view === "inbox") {
    content = (
      <Inbox
        inbox={inbox}
        check={check}
        assign={assign}
        assignError={assignError}
        filtered={filtered}
        onOpen={open}
        onRetry={() => {
          setTries((n) => n + 1);
        }}
      />
    );
  } else if (view === "ai") {
    content = <AiAnswers gaps={gaps} projectId={projectId} href={href} onOpenGap={open} onChanged={changed} version={version} />;
  } else {
    content = <NoiseReport gaps={gaps} projectId={projectId} />;
  }

  return (
    <>
      <ShellHeader
        tabs={
          <>
            <SegmentedControl
              label="View"
              options={VIEWS}
              value={view}
              testId="gaps-tab"
              onChange={(v) => {
                setView(VIEWS.find((x) => x.value === v)?.value ?? "inbox");
              }}
            />
            <div className="gaps-actions" data-testid="gaps-actions">
              <Button
                variant="glass"
                onClick={() => {
                  setRaising(true);
                }}
                data-testid="gaps-raise"
              >
                Raise a gap
              </Button>
              <ResolveButton
                view={staged.view}
                onOpen={() => {
                  setResolving(true);
                }}
              />
            </div>
          </>
        }
        tools={
          view === "inbox" ? (
            <Filters
              filters={filters}
              stories={stories}
              people={people}
              onChange={(patch) => {
                setFilters((f) => {
                  const next = { ...f, ...patch };
                  return JSON.stringify(next) === JSON.stringify(f) ? f : next; // the same choice again reads nothing
                });
              }}
            />
          ) : undefined
        }
      />
      <GapCheckStatus check={check} listed={view === "inbox" ? inbox.list.gaps.filter((g) => isNewGap(g, check)).length : 0} />
      <div className="gaps-body" ref={body}>
        {content}
      </div>
      {card ? <GapCard key={card.id} gap={card} isNew={isNewGap(card, check)} onClose={close} control={control} /> : null}
      {resolving ? (
        <ResolveDialog
          gaps={gaps}
          projectId={projectId}
          staged={staged.gaps}
          names={memberNames(people)}
          onClose={() => {
            setResolving(false);
          }}
          onResolved={changed}
        />
      ) : null}
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
