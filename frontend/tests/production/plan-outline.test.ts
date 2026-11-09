// FE-PLAN-06 (TECH-SPECS §2.15): the Plan tab outline and its run quality. Layer U (pure): the wire parse of quality, review, planner origin
// and not-buildable members; `groupByArea` (areas, then stories, then tasks); the quality notice sentences.
import { test } from "node:test";
import assert from "node:assert/strict";
import { toPlanSnapshot } from "@/lib/data/http/plan";
import { groupByArea } from "@/lib/domain/logic/plan";
import { isDataError } from "@/lib/domain/errors";
import type { PlanQuality } from "@/lib/domain/plan";
import { qualityLines } from "@/components/plan/tasks/task-logic";

const task = (id: string, area: string, story: string, over: Record<string, unknown> = {}) => ({
  task_id: id,
  area_id: area,
  story_id: story,
  phase: "logic",
  title: id,
  description: null,
  done_when: [],
  complexity: "low",
  status: "pending",
  implements: [],
  waits_for: [],
  ready: false,
  revision: 1,
  ...over,
});
const area = (id: string, kind: string, over: Record<string, unknown> = {}) => ({
  area_id: id,
  kind,
  title: id,
  description: null,
  status: "active",
  ...over,
});
const story = (id: string, areaId: string) => ({
  story_id: id,
  area_id: areaId,
  kind: "core",
  title: id,
  summary: null,
  progress: "pending",
  status: "active",
});
const body = (over: Record<string, unknown> = {}) => ({
  plan_id: "p",
  areas: [area("AREA::PROJECT", "project", { title: null }), area("AREA::B", "module"), area("AREA::A", "module")],
  stories: [story("S::B2", "AREA::B"), story("S::B1", "AREA::B"), story("S::A", "AREA::A"), story("S::P", "AREA::PROJECT")],
  tasks: [
    task("T1", "AREA::B", "S::B1"),
    task("T2", "AREA::PROJECT", "S::P", { planned_by: "rules" }),
    task("T3", "AREA::B", "S::B2"),
    task("T4", "AREA::A", "S::A"),
    task("T5", "AREA::GONE", "S::GONE"),
  ],
  ...over,
});

test("toPlanSnapshot reads the run quality, the review findings, who planned each task and the not-buildable members", () => {
  const plan = toPlanSnapshot(
    body({
      areas: [
        area("AREA::A", "module", {
          planned_by: "rules",
          not_buildable: [{ spec_id: "REQ::R", name: null, reason: "process", note: "A review step." }],
        }),
      ],
      quality: {
        run_id: "run-1",
        status: "partially_succeeded",
        fallbacks: { tasks: 3, project: 1, other: 9 },
        tasks_planned: 5,
        review: { blocking: 2 },
        review_failed: false,
        unplaced: 4,
        oversized: 1,
      },
      review: [{ area_id: "AREA::A", task_id: null, problem: "No task builds it.", severity: "blocking" }],
    }),
  );
  assert.deepEqual(plan.quality, {
    runId: "run-1",
    status: "partially_succeeded",
    fallbacks: { project: 1, tasks: 3 },
    review: { blocking: 2 },
    reviewFailed: false,
    unplaced: 4,
    oversized: 1,
  });
  assert.deepEqual(plan.review, [{ areaId: "AREA::A", taskId: null, problem: "No task builds it.", severity: "blocking" }]);
  assert.deepEqual(plan.areas[0].notBuildable, [{ specId: "REQ::R", name: null, reason: "process", note: "A review step." }]);
  assert.equal(plan.areas[0].plannedBy, "rules");
  assert.deepEqual(
    plan.tasks.map((t) => t.plannedBy),
    ["model", "rules", "model", "model", "model"],
  );
  // an older API without these fields: no quality, no review, a model-planned task, no not-buildable list
  const old = toPlanSnapshot(body());
  assert.equal(old.quality, null);
  assert.deepEqual(old.review, []);
  for (const bad of [{ quality: { run_id: "r", status: "failed" } }, { review: [{ area_id: "A", problem: "x", severity: "fatal" }] }]) {
    assert.throws(
      () => toPlanSnapshot(body(bad)),
      (e) => isDataError(e) && e.code === "invalid_response",
    );
  }
});

test("groupByArea: areas and stories in the snapshot's order, the project-wide area last, tasks kept, unknown ids get a stand-in", () => {
  const plan = toPlanSnapshot(body({ review: [{ area_id: "AREA::B", task_id: "T1", problem: "Too broad.", severity: "advisory" }] }));
  const groups = groupByArea(plan, plan.tasks);
  assert.deepEqual(
    groups.map((g) => g.area.areaId),
    ["AREA::B", "AREA::A", "AREA::PROJECT", "AREA::GONE"],
  );
  assert.deepEqual(
    groups[0].stories.map((s) => [s.story.storyId, s.tasks.map((t) => t.taskId)]),
    [
      ["S::B2", ["T3"]],
      ["S::B1", ["T1"]],
    ],
  );
  assert.deepEqual(
    groups[0].tasks.map((t) => t.taskId),
    ["T3", "T1"],
  );
  assert.equal(groups[0].review.length, 1);
  assert.equal(groups[1].review.length, 0);
  assert.equal(groups[3].stories[0].story.title, "S::GONE");
  assert.equal(groups.flatMap((g) => g.tasks).length, plan.tasks.length);
});

test("qualityLines: nothing for a clean run; fallbacks, an unplanned project step and review findings each get a sentence", () => {
  const clean: PlanQuality = {
    runId: "r",
    status: "succeeded",
    fallbacks: {},
    review: {},
    reviewFailed: false,
    unplaced: 0,
    oversized: 0,
  };
  assert.deepEqual(qualityLines(null, 0), []);
  assert.deepEqual(qualityLines(clean, 0), []);
  assert.deepEqual(qualityLines({ ...clean, review: { advisory: 1 } }, 0), [
    "The review found 0 blocking issues and 1 advisory issue; each is listed in its area.",
  ]);
  assert.deepEqual(qualityLines({ ...clean, status: "partially_succeeded", fallbacks: { tasks: 2, project: 1 }, reviewFailed: true }, 7), [
    "The planner could not answer 2 parts of the work, so 7 tasks were written from the specification by rules. They are marked Rule-made.",
    "The project-wide setup and closing work was not planned: the plan has only a workspace setup task and no closing tasks.",
    "The review could not run.",
  ]);
});
