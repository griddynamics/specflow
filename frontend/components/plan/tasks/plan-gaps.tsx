"use client";
// FR-GWEB-04 (api.md "User interface", Plan screens): the open gaps of the plan's tasks and stories, linked to their cards. The open gaps
// (status open, answered or applying; any route; up to 200) are read once when the Plan tab is shown; a task's gaps are those whose ABOUT
// ids (a node's spec id or one of its aliases) meet the spec ids the task implements, a story's the union of its tasks'. A failed read (or
// 503 gap_unavailable) shows nothing: the plan never breaks for it.
import Link from "next/link";
import { useEffect, useState } from "react";
import { gapLink } from "@/components/gaps/gap-links";
import { gapsForSpecIds, unionGaps } from "@/components/gaps/gaps-logic";
import type { Gap } from "@/lib/domain/gaps";
import type { PlanTask } from "@/lib/domain/plan";
import { useGapsPort, usePlanRoutes } from "../use-plan-ports";

/** The project's open gaps, read once; empty until (and unless) the read succeeds. */
export function useOpenGaps(projectId: string): Gap[] {
  const gaps = useGapsPort();
  const [open, setOpen] = useState<Gap[]>([]);
  useEffect(() => {
    let alive = true;
    gaps.list(projectId, { status: ["open", "answered", "applying"], limit: 200 }).then(
      (page) => {
        if (alive) setOpen(page.gaps);
      },
      () => undefined, // no gaps shown; the plan stays as it is
    );
    return () => {
      alive = false;
    };
  }, [gaps, projectId]);
  return open;
}

/** A task's open gaps. */
export const taskGaps = (gaps: readonly Gap[], task: PlanTask): Gap[] =>
  gapsForSpecIds(
    gaps,
    task.implements.map((i) => i.specId),
  );

/** A story's open gaps: the union of its tasks'. */
export const storyGaps = (gaps: readonly Gap[], tasks: readonly PlanTask[]): Gap[] => unionGaps(tasks.map((t) => taskGaps(gaps, t)));

/** The task body's list of its open gaps, each a link to its card, titled by the gap's title. */
export function TaskGapLinks({ projectId, gaps }: Readonly<{ projectId: string; gaps: readonly Gap[] }>) {
  const routes = usePlanRoutes();
  if (gaps.length === 0) return null;
  return (
    <section className="plan-gaps" aria-label="Open gaps" data-testid="plan-task-gaps-list">
      <h4 className="plan-gaps__title eyebrow">Open gaps</h4>
      <ul className="plan-gaps__list">
        {gaps.map((g) => (
          <li key={g.id} className="body-sm">
            <Link className="plan-gaps__link gdg-focus" href={gapLink(routes, projectId, g.id)} data-testid="plan-task-gap-link">
              {g.title === "" ? g.id : g.title}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
