import assert from "node:assert/strict";
import { test } from "node:test";
import { NODES } from "../lib/how-graph";
import { AGENTS, answerQuestion, count, initialPlan, nextRound, planSummary } from "../lib/plan-state";

test("the start: three areas done, three agents claim the first ready items, one rule waits for its question", () => {
  const plan = initialPlan();
  assert.equal(count(plan, "done"), 3);
  assert.equal(count(plan, "prog"), AGENTS.length);
  assert.equal(plan.status.n6, "block");
  assert.equal(new Set(Object.values(plan.holder)).size, AGENTS.length, "each item goes to exactly one agent");
});

test("a round finishes what the agents hold and hands them the next ready items", () => {
  const first = initialPlan();
  const second = nextRound(first);
  assert.equal(count(second, "done"), 3 + AGENTS.length);
  assert.equal(second.status.n6, "block", "the open question still blocks its item");
  for (const id of Object.keys(first.holder)) assert.equal(second.status[id as keyof typeof second.status], "done");
});

test("answering the open question unblocks its item", () => {
  const answered = answerQuestion(initialPlan());
  assert.notEqual(answered.status.n6, "block");
  assert.equal(answered.answered, true);
});

test("rounds end with everything built", () => {
  let plan = answerQuestion(initialPlan());
  for (let round = 0; round < NODES.length && count(plan, "done") < NODES.length; round++) plan = nextRound(plan);
  assert.equal(count(plan, "done"), NODES.length);
  assert.equal(planSummary(plan), "Everything is built.");
});

test("without an answer the blocked item and what depends on it never finish", () => {
  let plan = initialPlan();
  for (let round = 0; round < NODES.length; round++) plan = nextRound(plan);
  assert.equal(plan.status.n6, "block");
  assert.equal(plan.status.n9, "wait");
  assert.match(planSummary(plan), /1 blocked by an open question/);
});
