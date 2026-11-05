"use client";
// FE-PLAN-06.AC2, AC9 (DECISIONS P24-2): the outline of the Plan tab as a presentational component: work areas, then their stories, then the
// tasks (`groupByArea`; areas and stories without a visible task have no row), tasks in the port's order (phase order). Open state and the
// task body come from the caller; no ports here (testable on its own).
import type { ReactNode } from "react";
import { groupByArea, type StoryGroup } from "@/lib/domain/logic/plan";
import type { PlanSnapshot, PlanTask } from "@/lib/domain/plan";
import { AreaSection } from "./AreaSection";
import { StorySection } from "./StorySection";
import { TaskRow } from "./TaskRow";
import { visibleTasks } from "./task-logic";

export function PlanOutline({
  plan,
  openAreas,
  openStories,
  openTasks,
  onToggleArea,
  onToggleStory,
  onToggleTask,
  renderBody,
}: Readonly<{
  plan: PlanSnapshot;
  openAreas: ReadonlySet<string>;
  openStories: ReadonlySet<string>;
  openTasks: ReadonlySet<string>;
  onToggleArea: (areaId: string) => void;
  onToggleStory: (storyId: string) => void;
  onToggleTask: (taskId: string) => void;
  renderBody: (task: PlanTask) => ReactNode;
}>) {
  return (
    <>
      {groupByArea(plan, visibleTasks(plan.tasks)).map((g) => (
        <AreaSection
          key={g.area.areaId}
          group={g}
          open={openAreas.has(g.area.areaId)}
          onToggle={() => {
            onToggleArea(g.area.areaId);
          }}
        >
          {g.stories.map((s) => (
            <StoryRows
              key={s.story.storyId}
              group={s}
              open={openStories.has(s.story.storyId)}
              openTasks={openTasks}
              onToggle={onToggleStory}
              onToggleTask={onToggleTask}
              renderBody={renderBody}
            />
          ))}
        </AreaSection>
      ))}
    </>
  );
}

function StoryRows({
  group,
  open,
  openTasks,
  onToggle,
  onToggleTask,
  renderBody,
}: Readonly<{
  group: StoryGroup;
  open: boolean;
  openTasks: ReadonlySet<string>;
  onToggle: (storyId: string) => void;
  onToggleTask: (taskId: string) => void;
  renderBody: (task: PlanTask) => ReactNode;
}>) {
  return (
    <StorySection
      group={group}
      open={open}
      onToggle={() => {
        onToggle(group.story.storyId);
      }}
    >
      {group.tasks.map((t) => (
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
    </StorySection>
  );
}
