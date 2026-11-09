"use client";
// FE-PLAN-06, FE-PLAN-07 (TECH-SPECS §2.15; DECISIONS P24-*): the Plan tab. The `plan` prop is only the first paint: the tab re-reads
// `plan.state(projectId)` on mount, because `updateTaskBody` fires no event and the prop can be stale after a tab switch
// (Plan -> Develop -> Plan keeps an edit). Grouping and order come from the port and `groupByArea`; nothing is sorted here. Above the
// outline, the quality notice says when part of the plan was written by the fallback rules or the review found issues.
// The signature is the one P21 pinned: `plan` is declared and not read, so the first paint is a
// skeleton until the read settles (DECISIONS P24-6).
import { useCallback, useEffect, useRef, useState } from "react";
import { Skeleton } from "@/components/ui/Skeleton";
import { usePlanPort } from "../use-plan-ports";
import { groupByArea } from "@/lib/domain/logic/plan";
import type { PlanSnapshot, PlanTask } from "@/lib/domain/plan";
import { PlanOutline } from "./PlanOutline";
import { PlanQualityNotice } from "./PlanQualityNotice";
import { TaskBody } from "./TaskBody";
import { recallOpen, rememberOpen } from "./open-state";
import { visibleTasks } from "./task-logic";
import "./tasks.css";

export function PlanTab({ projectId }: Readonly<{ projectId: string; plan: PlanSnapshot }>) {
  const planPort = usePlanPort();
  const [snapshot, setSnapshot] = useState<PlanSnapshot | null>(null);
  const [failed, setFailed] = useState(false);
  // The first area with tasks and its first story start open (FE-PLAN-06.AC5), once, when the first read settles; the user's choices
  // live here afterwards. P26 (m4): the open sets come back from module memory after a tab switch (`open-state.ts`).
  const [openAreas, setOpenAreas] = useState<ReadonlySet<string>>(() => recallOpen(projectId)?.areas ?? new Set());
  const [openStories, setOpenStories] = useState<ReadonlySet<string>>(() => recallOpen(projectId)?.stories ?? new Set());
  const [openTasks, setOpenTasks] = useState<ReadonlySet<string>>(() => recallOpen(projectId)?.tasks ?? new Set());
  // Memory is written only once the user (or the first-phase default) has set the open state, never from the empty first render.
  const settled = useRef(recallOpen(projectId) !== undefined);
  useEffect(() => {
    if (settled.current) rememberOpen(projectId, { areas: openAreas, stories: openStories, tasks: openTasks });
  }, [projectId, openAreas, openStories, openTasks]);

  const refresh = useCallback(
    () =>
      planPort.state(projectId).then(
        (s) => {
          if (s.phase === "ready") setSnapshot(s.plan);
        },
        () => undefined, // the rows already show the saved task
      ),
    [planPort, projectId],
  );
  useEffect(() => {
    let alive = true;
    planPort.state(projectId).then(
      (s) => {
        if (!alive) return;
        if (s.phase !== "ready") {
          setFailed(true);
          return;
        } // not Stage 2 any more: no endless Skeleton
        setSnapshot(s.plan);
        if (!settled.current) {
          settled.current = true;
          const first = groupByArea(s.plan, visibleTasks(s.plan.tasks)).at(0);
          if (first) {
            setOpenAreas(new Set([first.area.areaId]));
            setOpenStories(new Set(first.stories.slice(0, 1).map((g) => g.story.storyId)));
          }
        }
      },
      () => {
        if (alive) setFailed(true);
      },
    );
    return () => {
      alive = false;
    };
  }, [planPort, projectId]);

  const toggle = <T,>(set: ReadonlySet<T>, key: T): Set<T> => {
    const next = new Set(set);
    if (!next.delete(key)) next.add(key);
    return next;
  };
  // A saved task replaces its row at once; the re-read refreshes the other tasks (their "Depends on" lists show titles).
  const onTaskSaved = useCallback(
    (task: PlanTask) => {
      setSnapshot((cur) => (cur ? { ...cur, tasks: cur.tasks.map((t) => (t.taskId === task.taskId ? task : t)) } : cur));
      void refresh();
    },
    [refresh],
  );

  if (!snapshot) {
    return (
      <div
        className="plan-tab"
        data-testid="plan-tab"
        data-project={projectId}
        aria-busy={failed ? undefined : true}
        data-loading={failed ? undefined : ""}
      >
        {failed ? (
          <p className="body-sm" role="status">
            Tasks could not be loaded.
          </p>
        ) : (
          <Skeleton variant="block" height={280} />
        )}
      </div>
    );
  }
  return (
    <div className="plan-tab" data-testid="plan-tab" data-project={projectId}>
      <PlanQualityNotice
        quality={snapshot.quality}
        ruleTasks={visibleTasks(snapshot.tasks).filter((t) => t.plannedBy === "rules").length}
      />
      <PlanOutline
        plan={snapshot}
        openAreas={openAreas}
        openStories={openStories}
        openTasks={openTasks}
        onToggleArea={(id) => {
          settled.current = true;
          setOpenAreas((s) => toggle(s, id));
        }}
        onToggleStory={(id) => {
          settled.current = true;
          setOpenStories((s) => toggle(s, id));
        }}
        onToggleTask={(id) => {
          settled.current = true;
          setOpenTasks((s) => toggle(s, id));
        }}
        renderBody={(t) => <TaskBody projectId={projectId} task={t} onSaved={onTaskSaved} />}
      />
    </div>
  );
}
