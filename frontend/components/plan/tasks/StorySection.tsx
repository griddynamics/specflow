"use client";
// FE-PLAN-06 (G-PLAN-2): one story inside a work area. The header is a `<button aria-expanded>` inside an h3 with the story's progress; the
// task list stays mounted with `hidden`. The summary (written by the describe planner) sits above the tasks.
import type { ReactNode } from "react";
import { Icon } from "@/components/ui/Icon";
import type { StoryGroup } from "@/lib/domain/logic/plan";
import { doneProgress } from "./task-logic";

export function StorySection({
  group,
  open,
  onToggle,
  children,
}: Readonly<{ group: StoryGroup; open: boolean; onToggle: () => void; children: ReactNode }>) {
  const { story, tasks } = group;
  const { done, total } = doneProgress(tasks);
  const listId = `plan-story-${story.storyId.replace(/[^\w-]/g, "-")}`;
  return (
    <div className="plan-story" data-testid="plan-story" data-story={story.storyId}>
      <h3 className="plan-story__heading">
        <button
          type="button"
          className="plan-story__head"
          data-testid="plan-story-head"
          aria-expanded={open}
          aria-controls={listId}
          onClick={onToggle}
        >
          <Icon name={open ? "caret-down" : "caret-right"} size={16} />
          <span className="plan-story__name" data-testid="plan-story-name">
            {story.title}
          </span>
          <span className="plan-story__count body-sm" data-testid="plan-story-count">{`${String(done)} of ${String(total)} done`}</span>
        </button>
      </h3>
      <div id={listId} hidden={!open}>
        {story.summary !== null && story.summary !== "" ? <p className="plan-story__summary body-sm">{story.summary}</p> : null}
        <ul className="plan-story__tasks">{children}</ul>
      </div>
    </div>
  );
}
