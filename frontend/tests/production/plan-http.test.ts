// FE-PLAN-01 (TECH-SPECS §2.15): the production plan port over `/projects/{pid}/plan*`.
// Layer U (pure): the wire parsers, the creating state, the events, the task Markdown. Layer P (the port against a fake `fetch`):
// state per stage, create with replan, polling from creating to ready or failed, leave, preview, the disabled edit.
import { test, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import {
  createHttpPlan,
  creatingOf,
  eventOf,
  toPlanPreview,
  toPlanSnapshot,
  toPlanStatus,
  type PlanStatusWire,
} from "@/lib/data/http/plan";
import { taskMarkdown } from "@/lib/domain/logic/plan";
import { isDataError } from "@/lib/domain/errors";
import type { PlanEvent, PlanState } from "@/lib/domain/plan";

const PID = "11111111-1111-4111-8111-111111111111";

const status = (over: Record<string, unknown> = {}) => ({
  status: "idle",
  has_plan: false,
  run_id: null,
  mode: null,
  replan: null,
  step: null,
  steps_done: 0,
  steps_total: 0,
  started_at: null,
  finished_at: null,
  unchanged: null,
  error_code: null,
  ...over,
});
const running = (step: string, done: number) => status({ status: "running", run_id: "run-1", step, steps_done: done, steps_total: 10 });
const wireTask = (over: Record<string, unknown> = {}) => ({
  task_id: "TASK::1",
  area_id: "AREA::A",
  story_id: "STORY::A",
  phase: "setup",
  title: "Set up the project",
  description: "Create the repository.",
  done_when: [{ text: "It builds.", source: "spec" }],
  complexity: "low",
  status: "pending",
  implements: [{ spec_id: "MOD::CORE", label: "Module", name: "Core" }],
  waits_for: [],
  ready: true,
  revision: 2,
  commit: null,
  block_reason: null,
  has_open_questions: false,
  ...over,
});
const content = (tasks = [wireTask()]) => ({
  plan_id: "plan-1",
  last_build_id: "build-1",
  updated_at: "2026-11-05T09:00:00Z",
  areas: [{ area_id: "AREA::A", kind: "module", title: "Core", description: null, status: "active" }],
  stories: [
    { story_id: "STORY::A", area_id: "AREA::A", kind: "setup", title: "Setup", summary: null, progress: "pending", status: "active" },
  ],
  tasks,
});

// ---- Layer U ----

test("toPlanStatus reads the run state and refuses another shape", () => {
  assert.deepEqual(toPlanStatus(running("tasks", 4)), {
    status: "running",
    hasPlan: false,
    runId: "run-1",
    step: "tasks",
    stepsDone: 4,
    stepsTotal: 10,
    errorCode: null,
  });
  for (const bad of [null, [], status({ status: "done" }), status({ steps_done: -1 }), status({ has_plan: "yes" })]) {
    assert.throws(
      () => toPlanStatus(bad),
      (e) => isDataError(e) && e.code === "invalid_response",
    );
  }
});

test("creatingOf: the step being run, 1-based and never past the total, with its label", () => {
  const wire = (step: string | null, stepsDone: number): PlanStatusWire => ({
    status: "running",
    hasPlan: false,
    runId: "r",
    step,
    stepsDone,
    stepsTotal: 10,
    errorCode: null,
  });
  assert.deepEqual(creatingOf(wire("snapshot", 0)), {
    phase: "creating",
    runId: "r",
    step: 1,
    total: 10,
    label: "Reading the knowledge graph",
  });
  assert.equal(creatingOf(wire("check", 10)).step, 10);
  assert.equal(creatingOf(wire(null, 0)).label, "Reading the knowledge graph");
  assert.equal(creatingOf(wire("future_step", 3)).label, "future_step");
});

test("eventOf names the change a listener sees", () => {
  const creating = (step: number): PlanState => ({ phase: "creating", runId: "r", step, total: 10, label: `s${String(step)}` });
  const ready: PlanState = { phase: "ready", plan: { planId: "p", areas: [], stories: [], tasks: [] } };
  assert.equal(eventOf(null, creating(1)), "plan.started");
  assert.equal(eventOf({ phase: "idle" }, creating(1)), "plan.started");
  assert.equal(eventOf(creating(1), creating(1)), null);
  assert.equal(eventOf(creating(1), creating(2)), "plan.step");
  assert.equal(eventOf(creating(2), ready), "plan.ready");
  assert.equal(eventOf(creating(2), { phase: "idle", failure: "x" }), "plan.failed");
  assert.equal(eventOf({ phase: "idle" }, ready), null);
});

test("toPlanSnapshot maps the plan and gives each task its Markdown body", () => {
  const plan = toPlanSnapshot(
    content([wireTask(), wireTask({ task_id: "TASK::2", phase: "logic", title: "Pay", waits_for: ["TASK::1"] })]),
  );
  assert.equal(plan.planId, "plan-1");
  assert.deepEqual(plan.areas, [{ areaId: "AREA::A", kind: "module", title: "Core", description: null }]);
  assert.equal(plan.stories[0].progress, "pending");
  const [first, second] = plan.tasks;
  assert.equal(first.version, 2);
  assert.deepEqual(first.implements, [{ specId: "MOD::CORE", name: "Core", label: "Module" }]);
  assert.deepEqual(first.doneWhen, ["It builds."]);
  assert.match(second.body, /## Depends on\n- Set up the project\n/);
  assert.throws(
    () => toPlanSnapshot(content([wireTask({ phase: "BOOTSTRAP" })])),
    (e) => isDataError(e) && e.code === "invalid_response",
  );
});

test("taskMarkdown: title, description, done when, then the graph sections; empty sections left out", () => {
  const md = taskMarkdown(
    {
      title: "Pay",
      description: "Take the payment.",
      doneWhen: ["Paid."],
      implements: [
        { specId: "FEAT::PAY", name: "Payment", label: "Feature" },
        { specId: "REQ::GONE", name: null, label: null },
      ],
      waitsFor: ["TASK::1", "TASK::X"],
    },
    new Map([["TASK::1", "Set up"]]),
  );
  assert.equal(
    md,
    "# Pay\n\nTake the payment.\n\n## Done when\n- Paid.\n\n## Implements\n- Payment (Feature)\n- REQ::GONE\n\n## Depends on\n- Set up\n- TASK::X\n",
  );
  assert.equal(taskMarkdown({ title: "T", description: "", doneWhen: [], implements: [], waitsFor: [] }, new Map()), "# T\n");
});

test("toPlanPreview reads the features and the estimate; the estimate may be unknown", () => {
  assert.deepEqual(toPlanPreview({ features: [{ spec_id: "F", name: "Pay" }], calls: 3, input_tokens: 900 }), {
    features: [{ specId: "F", name: "Pay" }],
    calls: 3,
    inputTokens: 900,
  });
  assert.deepEqual(toPlanPreview({ features: [], calls: null, input_tokens: null }), { features: [], calls: null, inputTokens: null });
});

// ---- Layer P ----

type Route = (method: string, path: string, body: unknown) => { status: number; body: unknown };
const calls: { method: string; path: string; body: unknown }[] = [];

function fakeFetch(route: Route) {
  calls.length = 0;
  mock.method(globalThis, "fetch", (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const path = url.replace(/^\/api\/backend/, "");
    const body: unknown = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body });
    const answer = route(method, path, body);
    return Promise.resolve(new Response(JSON.stringify(answer.body), { status: answer.status }));
  });
}

afterEach(() => {
  mock.restoreAll();
  mock.timers.reset();
});

test("state: idle without a plan, idle with the code of a failed run, creating while a run goes, ready with the plan", async () => {
  const port = createHttpPlan();
  let answer: unknown = status();
  fakeFetch((_m, path) => (path.endsWith("/plan/tasks") ? { status: 200, body: content() } : { status: 200, body: answer }));

  assert.deepEqual(await port.state(PID), { phase: "idle" });
  answer = status({ status: "failed", error_code: "no_spec_graph" });
  assert.deepEqual(await port.state(PID), { phase: "idle", failure: "no_spec_graph" });
  answer = running("describe", 2);
  assert.deepEqual(await port.state(PID), { phase: "creating", runId: "run-1", step: 3, total: 10, label: "Describing the work areas" });
  answer = status({ status: "succeeded", has_plan: true });
  const ready = await port.state(PID);
  assert.ok(ready.phase === "ready");
  assert.equal(ready.plan.tasks.length, 1);
  port.leave(PID);
});

test("create posts replan, starts creating at once and polls to ready; leave stops the polling", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const port = createHttpPlan({ pollMs: 1000 });
  const answers = [running("partition", 1), running("tasks", 4), status({ status: "succeeded", has_plan: true })];
  fakeFetch((method, path) => {
    if (method === "POST") return { status: 202, body: running("snapshot", 0) };
    if (path.endsWith("/plan/tasks")) return { status: 200, body: content() };
    return { status: 200, body: answers.shift() };
  });
  const seen: { type: PlanEvent["type"]; phase: PlanState["phase"] }[] = [];
  const off = port.subscribe(PID, (e, next) => seen.push({ type: e.type, phase: next.phase }));

  const { runId } = await port.create(PID, { replan: true });
  assert.equal(runId, "run-1");
  assert.deepEqual(calls[0], { method: "POST", path: `/projects/${PID}/plan`, body: { replan: true } });
  for (let i = 0; i < 3; i++) {
    mock.timers.tick(1000);
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
  }

  assert.deepEqual(
    seen.map((s) => s.type),
    ["plan.started", "plan.step", "plan.step", "plan.ready"],
  );
  assert.equal(seen.at(-1)?.phase, "ready");
  const reads = calls.length;
  mock.timers.tick(5000);
  await new Promise((r) => setImmediate(r));
  assert.equal(calls.length, reads, "no poll once the plan is ready");
  off();
  port.leave(PID);
});

test("a run that fails while creating moves the listener back to idle with its code", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const port = createHttpPlan({ pollMs: 500 });
  fakeFetch((method) =>
    method === "POST"
      ? { status: 202, body: running("snapshot", 0) }
      : { status: 200, body: status({ status: "failed", error_code: "graph_changed" }) },
  );
  const seen: PlanState[] = [];
  port.subscribe(PID, (_e, next) => seen.push(next));

  await port.create(PID);
  mock.timers.tick(500);
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  assert.deepEqual(seen.at(-1), { phase: "idle", failure: "graph_changed" });
  port.leave(PID);
});

test("create rejects with the API's refusal; preview reads the features; updateTaskBody is not available", async () => {
  const port = createHttpPlan();
  fakeFetch((method) =>
    method === "POST"
      ? { status: 409, body: { error: { code: "no_graph_build", message: "x" } } }
      : { status: 200, body: { features: [{ spec_id: "F", name: "Pay" }], calls: null, input_tokens: null } },
  );

  await assert.rejects(port.create(PID), (e) => isDataError(e) && e.code === "conflict" && e.detail?.apiCode === "no_graph_build");
  assert.deepEqual((await port.preview(PID)).features, [{ specId: "F", name: "Pay" }]);
  await assert.rejects(port.updateTaskBody(PID, "t", { start: 0, end: 1 }, "x", 1), (e) => isDataError(e) && e.code === "not_available");
});
