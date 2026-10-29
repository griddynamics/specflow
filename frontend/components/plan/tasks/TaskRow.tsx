"use client";
// FE-PLAN-06.AC8 (G-PLAN-5; DECISIONS P24-2, P27; Q20): one task row. The button shows the title and the status Tag (the word is always there,
// never colour alone) and no other tag (the Feature and Requirement tags are gone); activating it opens or closes the task body below it.
import type { ReactNode } from "react";
import { Icon } from "@/components/ui/Icon";
import { Tag } from "@/components/ui/Tag";
import type { PlanTask } from "@/lib/domain/plan";
import { STATUS_TAGS } from "./task-logic";

export function TaskRow({
  task,
  open,
  onToggle,
  children,
}: Readonly<{ task: PlanTask; open: boolean; onToggle: () => void; children?: ReactNode }>) {
  const status = STATUS_TAGS[task.status];
  const bodyId = `plan-task-${task.taskId.replace(/[^\w-]/g, "-")}`;
  return (
    <li className="plan-task" data-testid="plan-task" data-task={task.taskId} data-status={task.status}>
      <button
        type="button"
        className="plan-task__head"
        data-testid="plan-task-head"
        aria-expanded={open}
        aria-controls={open ? bodyId : undefined}
        onClick={onToggle}
      >
        <Icon name={open ? "caret-down" : "caret-right"} size={16} />
        <span className="plan-task__title" data-testid="plan-task-title">
          {task.title}
        </span>
        {status ? (
          <Tag tone={status.tone} className="plan-task__status">
            {status.label}
          </Tag>
        ) : null}
      </button>
      {open ? (
        <div id={bodyId} className="plan-task__body">
          {children}
        </div>
      ) : null}
    </li>
  );
}
