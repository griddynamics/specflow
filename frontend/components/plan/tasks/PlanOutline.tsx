"use client";
// FE-PLAN-06.AC2, AC9 (DECISIONS P24-2): the phase list of the Plan tab as a presentational component: groups from `groupByPhase` (empty phases
// have no group, so no row), rows in the port's order. Open state and the task body come from the caller; no ports here (testable on its own).
import type { ReactNode } from "react";
import { groupByPhase } from "@/lib/domain/logic/plan";
import type { PlanTask, TaskPhase } from "@/lib/domain/plan";
import { PhaseSection } from "./PhaseSection";
import { TaskRow } from "./TaskRow";
import { visibleTasks } from "./task-logic";

export function PlanOutline({
  tasks,
  openPhases,
  openTasks,
  onTogglePhase,
  onToggleTask,
  renderBody,
}: Readonly<{
  tasks: PlanTask[];
  openPhases: ReadonlySet<TaskPhase>;
  openTasks: ReadonlySet<string>;
  onTogglePhase: (phase: TaskPhase) => void;
  onToggleTask: (taskId: string) => void;
  renderBody: (task: PlanTask) => ReactNode;
}>) {
  return (
    <>
      {groupByPhase(visibleTasks(tasks)).map((g) => (
        <PhaseSection
          key={g.phase}
          phase={g.phase}
          tasks={g.tasks}
          open={openPhases.has(g.phase)}
          onToggle={() => {
            onTogglePhase(g.phase);
          }}
        >
          {g.tasks.map((t) => (
            <TaskRow
              key={t.taskId}
              task={t}
              open={openTasks.has(t.taskId)}
              onToggle={() => {
                onToggleTask(t.taskId);
              }}
            >
              {renderBody(t)}
            </TaskRow>
          ))}
        </PhaseSection>
      ))}
    </>
  );
}
