---
type: Explainer
title: "How the graph plan works"
description: "Why a plan on the graph, a first plan step by step, agents over MCP, and spec changes"
status: stable
tags: [graph, plan]
---
# how-it-works.md: how graph.plan turns the spec graph into a plan, keeps it current, and serves it to coding agents over MCP

An explainer for engineers and product people. The reference files hold the exact contracts:
[plan.md](plan.md) (steps), [data-model.md](data-model.md) (nodes, ids, stories),
[lifecycle.md](lifecycle.md) (task states, claims, briefs, agent contract), [api.md](api.md) (calls and
MCP tools). The decisions behind them are in [questions.md](questions.md).

## Why a plan on top of the graph

A coding agent cannot take a whole specification as context, and a plan written once as a document
goes stale at the first spec edit. The spec graph already holds what a plan needs. Modules are the
parts to build. Interfaces say who provides a contract and who consumes it. Data entities say what is
stored where. Features and requirements are the work, and stated dependencies give an order. Stable
`spec_id`s survive document edits. `graph.plan` turns that structure into stories and tasks, and stores
them in the same graph, linked to the spec nodes they implement. A spec change then maps to the exact
tasks it touches.

Planning needs judgement: how to cut work, what to set up first, how to describe a component. So model
calls write that content. Everything that must always hold is code: which area and story a node belongs
to, task ids, coverage, the waits between areas, the absence of cycles, which tasks may be claimed, and
what happens to finished work. Every model answer is checked, and a failed answer falls back to fixed
rules, so a plan run never fails because of a model.

## The shop example

The spec describes an online shop. After the build, the graph holds:

- **Modules.** "Orders service" and "Payments service" (backend), "Web shop" (frontend), "Orders
  database" (a datastore), "Shop cloud" (a deployment module the services are DEPLOYED_TO), and the
  external "Stripe".
- **Interfaces.** "Orders API" is PROVIDED_BY Orders service and CONSUMED_BY Web shop. "Stripe charges
  API" is PROVIDED_BY Stripe and CONSUMED_BY Payments service.
- **Data entity.** "Order" is STORED_IN Orders database. The data requirement "An order keeps its lines,
  total and currency" is PART_OF it.
- **Features.** "Place order" and "Order history export" in Orders service, "Card payment" in Payments
  service, "Checkout" in Web shop. The text says checkout needs card payment, so "Checkout" DEPENDS_ON
  "Card payment" (stated).
- **Requirements** under these owners, for example "A customer can place an order from the cart"
  (part of "Place order"), "Web shop places orders through the Orders API" (part of the interface), and
  the constraint "Stripe limits charges to 100 per second", which is PART_OF Stripe.
- **Cross-cutting rules.** The capability "Authentication", realised by Orders service and Web shop;
  the constraint "All services run in the EU region"; the release rule "A release needs the product
  owner's sign-off".
- **An open question.** "Can a customer cancel a paid order?" RELATES_TO the place-order requirement.

The host runs a plan after the build has returned, unless the build's settlement says documents are
still incomplete: a plan of half the documents would miss work. A backlog of inference work is no reason
to wait; nodes it places later simply move area, and their open tasks move with them.

## A first plan, step by step

1. **Snapshot.** The plan reads the plannable nodes and every edge touching them, with how certain each
   edge is (stated, implied or inferred). There is no `Plan` yet, so the mode is `first`.

2. **Partition.** Every internal module anchors an area: `AREA::ORDERS_SERVICE`,
   `AREA::PAYMENTS_SERVICE`, `AREA::WEB_SHOP`, `AREA::ORDERS_DATABASE`, `AREA::SHOP_CLOUD`, plus
   `AREA::PROJECT`. Stripe is external and gets no area. Then hubs are placed first:
   - "Orders API" goes to its provider, the orders area;
   - "Stripe charges API" has an external provider, so it goes to its single internal consumer, the
     payments area. The Stripe constraint follows by the same rule: it limits the code that talks to
     Stripe, and that code lives in the payments area;
   - "Order" goes to the area of the module that stores it, the orders database area.

   Every other node follows its owner: the interface's integration requirement joins the orders area,
   the order's data requirement joins the orders database area. "Authentication" is realised by two
   modules, so it is cross-cutting and goes to the project area, like the EU-region constraint and the
   release rule.

   Partition then creates the **stories**, also by fixed rules. Each feature, interface and data entity
   anchors one: `STORY::ORDERS_SERVICE::PLACE_ORDER`, `STORY::ORDERS_SERVICE::ORDERS_API`,
   `STORY::ORDERS_DATABASE::ORDER`, `STORY::WEB_SHOP::CHECKOUT`, and so on. What a module owns directly
   goes to its `CORE` story; the deployment area's infrastructure requirements are
   `STORY::SHOP_CLOUD::CORE`. The project area gets one story per document chapter for its
   cross-cutting rules (for example `STORY::PROJECT::CH_SHOP_SPEC_SECURITY`), plus `SETUP` and
   `CLOSING`. Story ids follow their anchors, so they survive re-planning.

3. **Describe.** One call per area writes a title, what the area owns and what it does not, and a
   summary of each interface it provides. The interfaces themselves come from the spec ("Orders API"),
   not from the model. The describer adds an interface only when the text implies one but names none,
   and marks it as model-written. It also writes a short summary of each story.

4. **Project.** One call writes the project profile: language, layout, test command, and conventions
   every task follows. "All services run in the EU region" becomes a convention citing its
   requirement, instead of a task. The same call plans the setup tasks (`TASK::PROJECT::WORKSPACE`,
   then toolchain, test harness, CI) and the closing tasks, such as an end-to-end checkout test.

5. **Tasks.** One call per planning part. A part is made of whole stories, so a story's tasks are
   planned together. In the orders area the planner writes, for example, `CONTRACT_ORDERS_API` (story
   Orders API), `LOGIC_PLACE_ORDER` and `QUALITY_PLACE_ORDER_LATENCY` (story Place order). Each task
   names its story, the `spec_id`s it implements, its waits inside the area, and done-when statements.
   A done-when item taken from the spec's acceptance criteria is marked `spec`; one the planner wrote
   from the statement is marked `planner`, so an agent knows which ones the spec guarantees. Every
   provided interface gets a contract task and every data entity a data task
   (`TASK::ORDERS_DATABASE::DATA_ORDER`). The release rule is not code: the planner of the project area's
   chapter story returns it as not buildable, with reason `governance`, and coverage accepts that.

6. **Stitch.** Code adds the waits between areas, from explicit edges, and always in one direction:
   - every area's first tasks wait for the setup tasks;
   - the web shop's integration task waits for `CONTRACT_ORDERS_API`, because the web shop consumes
     the Orders API. The orders area does not wait for the web shop;
   - orders service tasks that work with "Order" wait for `DATA_ORDER` in the orders database area;
   - the web shop's checkout tasks wait for the payments area's card-payment tasks, because "Checkout"
     DEPENDS_ON "Card payment" is stated. An inferred dependency would place work but never create a
     wait: a wrong wait would silently block parallel work;
   - the deployment area waits for setup, and the closing end-to-end test waits for the deployment and
     for every area it covers.

   If a cycle appears, the last-added stitch wait in it is dropped and counted. The result is one
   acyclic task graph: the plan DAG.

7. **Review.** The review model (the plan run can set one with `PlanOptions.review_model`) reads the whole plan
   and names gaps: a requirement no task can build, logic before its data, an oversized task. The issues
   are reported, not acted on.

8. **Diff, persist, check.** The plan is written: `Plan`, `WorkArea`s, `Story`s, `Task`s, and the
   relationships `IN_AREA`, `IN_STORY`, `IMPLEMENTS` and `WAITS_FOR`. The checks confirm that every node
   has one area, every open task is in one active story of its own area, every required node is covered,
   and the task graph has no cycle.

## Working through MCP

Users connect their coding agents to the app over MCP. The MCP server is host code: its tools call the
plan's reads and lifecycle calls, and the plan enforces the rules, so they hold for every client.

**Pick a story.** A user lists the web shop's stories and opens `STORY::WEB_SHOP::CHECKOUT`. `story`
shows the "Checkout" feature with all its requirements, including any that no task covers yet, and its
tasks with their status. Story progress is derived from the tasks: pending, in progress, blocked or
completed. `story_work` shows what finishing the story really takes: its open tasks plus everything
they still wait for, in order, with who holds each. Here that is the setup tasks, `CONTRACT_ORDERS_API`
and the card-payment tasks.

**Claim the next task.** The user tells an agent "implement Checkout". The agent calls
`claim_next(CHECKOUT)`. On a fresh plan nothing in the story is ready, so the call is refused with
`nothing_ready` and the list of blockers. The agent can take a blocker first (claim
`TASK::PROJECT::WORKSPACE`, which is ready), or leave the story to another agent. There are no forced
claims: building checkout against an Orders API that does not exist yet would build on nothing. Once
the blockers are done, `claim_next` claims the story's first ready task in dependency order. Exactly one
of concurrent claims succeeds, and no claim succeeds while a plan run is writing.

**Read the brief.** The agent reads the task brief, computed on request with no model call:
- the task, its done-when items with their source, and its story and area, with the area's interfaces;
- the project profile with its conventions;
- the requirements it implements, with their source lines;
- the finished prerequisites with their commits, for example the commit of `CONTRACT_ORDERS_API`;
- neighbouring spec nodes, and how certain the edges behind the task are;
- the open question "Can a customer cancel a paid order?", because it concerns an implemented
  requirement.

The brief never holds the whole graph.

**Report.** The agent builds the task and calls `complete_task` with the commit that holds the
integrated work, never an intermediate one. The tasks waiting for it become ready. When the open
question matters for the code, the agent blocks the task with `spec_ambiguous` instead of guessing;
other tasks go on. It fails a task it gives up on, and releases one it stops without a result. When a
brief says `spec_changed` or `missing`, the agent releases the task and asks for a plan run. After a
crash, the host finds stale claims and completes or releases them.

At any time `plan_dag` gives the plan as JSON or a Mermaid chart: the tasks in dependency order,
grouped by story. Story order is shown as a projection of the task graph. It may contain cycles (one
story's contract before another's logic, and the other way round), which are drawn as groups and never
enforced.

## When the spec changes

The plan does not compare texts to find what changed. The build classifies every change in a change
report, and the host passes the plan the reports of every build since the plan's last one. A missing
report stops the run with `change_reports_missing`; the host then passes it, or asks for a full
`replan`.

Say two edits land in one build:
- a document adds "A customer can cancel an order within 30 minutes" under "Place order";
- the threshold of "Orders over 1000 EUR need a manual review" changes to 500 EUR.

At the same time, re-extraction rewords the statement of "A customer can place an order from the cart"
without changing what it says.

The build reports the new requirement as `added`, the threshold rule as `changed_meaning`, the
"Place order" feature as `changed_links` (it gained a requirement), and the reworded requirement as
`changed_evidence`. The next plan run:

- ignores `changed_evidence`: rewording supersedes no finished work;
- re-plans only the orders area part holding the "Place order" story, and describes the orders area
  again. Every other area and the project plan reuse their stored answers, with no model call;
- shows the planner the current tasks, so unchanged work keeps its action;
- adds `LOGIC_CANCEL_ORDER` to the "Place order" story. The story was completed, so its derived
  progress goes back to in progress without any write to the story;
- supersedes the completed task implementing the threshold rule: it becomes `superseded`, a new
  revision `_R2` is created pending, and the old commit is listed as stale, with `changed_meaning` as
  the cause. Completed tasks waiting on it inside the area are superseded too; completed tasks of other
  areas are only reported;
- leaves a task an agent is running untouched, and lists it if its spec changed.

Between the build and the plan run, the brief of the threshold task already shows `spec_changed`, so an
agent does not start outdated work.

If no report marks a meaning or link change of any node, and nothing is open from the previous
run, the run stops after Snapshot with no model call. The plan's update is only as good as the build's
classification: a real change the build calls rewording leaves finished work in place, so superseded
tasks are reported with the class that caused them for the host to check.

## What the plan does not do

It does not run agents, git or tests, and has no executor: agents work through MCP. It does not change
the spec graph, does not judge whether the spec is complete or consistent (that is `graph.gap`), and
does not answer open questions. It does not filter or order work by release, does not allow a claim
that skips prerequisites, and does not estimate the time or cost of the work; `estimate_plan` estimates
only the calls and tokens of a plan run.
