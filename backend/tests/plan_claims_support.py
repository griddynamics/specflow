"""An in-memory stand-in for the graph store under the plan's claim calls (offline tests of the story claims).

``MemoryPlanExecutor`` implements the ``Executor`` surface the plan library uses (``read``, ``write``, ``tx``) over
dictionaries shaped like the plan queries' rows. It answers the plan read queries and runs the lifecycle statements
(``CLAIM_TASK``, ``TRANSITION_TASK``) and the story-call statements (``LOCK_STORIES``, ``LOCK_TASKS``, ``SET_STORIES``,
``SET_TASKS``) by name with the semantics of their Cypher and SQL files: a lock statement takes a per-node lock held to
the end of the transaction; a transaction's writes go to its own overlay, which its reads see over the committed rows
and which is applied when it ends without an error (dropped when it raises), so other calls read committed rows only.
Every statement yields to the event loop once, so concurrent calls really interleave.

``mini_plan`` builds the plan the claim tests share (see its docstring for the stories).
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

from graph.core.queries import Query

T0: Final = "2026-01-01T10:00:00+00:00"
_RELATIONS: Final = ("IN_AREA", "IN_STORY", "IMPLEMENTS", "WAITS_FOR", "SUPERSEDES")


def task_row(task_id: str, story_id: str, status: str = "pending", **values: Any) -> dict[str, Any]:
    """A task row as ``plan_read_tasks`` returns it."""

    area = story_id.split("::")[1]
    row: dict[str, Any] = {
        "task_id": task_id,
        "area_id": f"AREA::{area}",
        "action": task_id.rsplit("::", 1)[-1],
        "revision": 1,
        "story_id": story_id,
        "phase": "logic",
        "title": f"Title of {task_id}",
        "description": None,
        "done_when": [],
        "complexity": "medium",
        "implements": [],
        "release": [],
        "has_open_questions": False,
        "planned_by": "model",
        "prompt_ref": None,
        "spec_build_id": None,
        "status": status,
        "attempts": 0,
        "created_at": T0,
        "updated_at": T0,
    }
    return {**row, **values}


def story_row(story_id: str, **values: Any) -> dict[str, Any]:
    """A story row as ``plan_read_stories`` returns it."""

    area = story_id.split("::")[1]
    row: dict[str, Any] = {
        "story_id": story_id,
        "area_id": f"AREA::{area}",
        "kind": "feature",
        "title": f"Title of {story_id}",
        "status": "active",
        "created_at": T0,
        "updated_at": T0,
    }
    return {**row, **values}


def area_row(area_id: str, kind: str = "module", **values: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "area_id": area_id,
        "kind": kind,
        "title": f"Area {area_id}",
        "planned_by": "model",
        "status": "active",
        "created_at": T0,
        "updated_at": T0,
    }
    return {**row, **values}


Pair = tuple[str, str]
Overlay = dict[str, dict[str, dict[str, Any]]]


class Gate:
    """A pause in front of one statement: ``reached`` is set when a call gets there, the call goes on after ``go``."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.go = asyncio.Event()


class MemoryPlanExecutor:
    """The plan of one project in dictionaries, behind the executor surface.

    Test hooks: ``pause_before(name)`` stops the next call of that statement at a ``Gate``; ``fail_once(name)`` makes
    the next call of that statement raise ``RuntimeError``; ``lock_rounds`` records the story ids of every
    ``plan_lock_stories``; ``waits_view`` (when set) maps the stored ``WAITS_FOR``
    pairs to the pairs a read sees, told where the read runs (``"tx"`` inside a transaction, ``"read"`` outside)."""

    def __init__(self, project_id: str = "shop") -> None:
        self.project_id = project_id
        self.plan: dict[str, Any] | None = {
            "plan_id": "PLAN::MAIN",
            "consumed_build_ids": ["b1"],
            "applying": None,
            "created_at": T0,
            "updated_at": T0,
        }
        self.areas: dict[str, dict[str, Any]] = {}
        self.stories: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.waits: set[Pair] = set()
        self.statements: list[str] = []
        self.transactions = 0
        self.lock_rounds: list[tuple[str, ...]] = []
        self.waits_view: Callable[[set[Pair], str], set[Pair]] | None = None
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._gates: dict[str, Gate] = {}
        self._failures: set[str] = set()

    # Building the plan

    def add_waits(self, *pairs: Pair) -> None:
        self.waits.update(pairs)

    def set_task(self, task_id: str, **values: Any) -> None:
        self.tasks[task_id] = _merge(self.tasks[task_id], values)

    def set_story(self, story_id: str, **values: Any) -> None:
        self.stories[story_id] = _merge(self.stories[story_id], values)

    # Test hooks

    def pause_before(self, name: str) -> Gate:
        gate = self._gates[name] = Gate()
        return gate

    def fail_once(self, name: str) -> None:
        self._failures.add(name)

    def held_locks(self) -> list[tuple[str, str]]:
        return sorted(key for key, lock in self._locks.items() if lock.locked())

    # The Executor surface

    async def read(self, query: Query, project_id: str, /, **_params: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        assert project_id == self.project_id
        return self._answer(query, None)

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        assert project_id == self.project_id
        held: list[asyncio.Lock] = []
        try:
            return await self._run(query, params, None, held)
        finally:
            for lock in held:
                lock.release()

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[_Transaction]:
        assert project_id == self.project_id
        self.transactions += 1
        transaction = _Transaction(self)
        try:
            yield transaction
            transaction.commit()
        finally:
            transaction.release()

    # Statements

    def _answer(self, query: Query, overlay: Overlay | None) -> list[dict[str, Any]]:
        name = query.name
        self.statements.append(name)
        if name == "plan_read_plan":
            return [copy.deepcopy(self.plan)] if self.plan is not None else []
        if name == "plan_read_areas":
            return [copy.deepcopy(self.areas[key]) for key in sorted(self.areas)]
        if name == "plan_read_stories":
            stories = self._seen("stories", overlay)
            return [copy.deepcopy(stories[key]) for key in sorted(stories)]
        if name == "plan_read_tasks":
            tasks = self._seen("tasks", overlay)
            return [copy.deepcopy(tasks[key]) for key in sorted(tasks)]
        if name.startswith("plan_read_links_"):
            relation = name.removeprefix("plan_read_links_").upper()
            return self._links(relation, "read" if overlay is None else "tx")
        raise AssertionError(f"the memory executor does not read {name}")

    def _seen(self, kind: str, overlay: Overlay | None) -> dict[str, dict[str, Any]]:
        """The committed rows, with the transaction's own writes over them."""

        committed = self.stories if kind == "stories" else self.tasks
        return {**committed, **(overlay or {}).get(kind, {})}

    def _links(self, relation: str, where: str) -> list[dict[str, Any]]:
        assert relation in _RELATIONS
        if relation != "WAITS_FOR":
            return []
        waits = self.waits if self.waits_view is None else self.waits_view(set(self.waits), where)
        return [
            {
                "type": relation,
                "source": source,
                "target": target,
                "edge_key": f"{source}|WAITS_FOR|{target}",
                "properties": [],
            }
            for source, target in sorted(waits)
        ]

    async def _lock(self, kind: str, key: str, held: list[asyncio.Lock]) -> None:
        lock = self._locks.setdefault((kind, key), asyncio.Lock())
        await lock.acquire()
        held.append(lock)

    async def _run(
        self, query: Query, params: Mapping[str, Any], overlay: Overlay | None, held: list[asyncio.Lock]
    ) -> list[dict[str, Any]]:
        name = query.name
        self.statements.append(name)
        if (gate := self._gates.pop(name, None)) is not None:
            gate.reached.set()
            await gate.go.wait()
        if name in self._failures:
            self._failures.discard(name)
            raise RuntimeError(f"{name} failed")
        if name == "plan_lock_stories":
            return await self._lock_stories(params["story_ids"], held)
        if name == "plan_lock_tasks":
            return await self._lock_tasks(params["story_ids"], held)
        if name in ("plan_set_stories", "plan_set_tasks"):
            kind = "stories" if name == "plan_set_stories" else "tasks"
            return self._set_rows(kind, params, overlay)
        assert overlay is None, f"the memory executor runs {name} outside a transaction only"
        if name == "plan_claim_task":
            return await self._claim_task(params, held)
        if name == "plan_transition_task":
            return await self._transition(params, held)
        raise AssertionError(f"the memory executor does not run {name}")

    async def _lock_stories(self, story_ids: list[str], held: list[asyncio.Lock]) -> list[dict[str, Any]]:
        self.lock_rounds.append(tuple(story_ids))
        rows = []
        for story_id in story_ids:
            if story_id in self.stories:
                await self._lock("story", story_id, held)
                rows.append({"story_id": story_id, "story": copy.deepcopy(self.stories[story_id])})
        return rows

    async def _lock_tasks(self, story_ids: list[str], held: list[asyncio.Lock]) -> list[dict[str, Any]]:
        rows = []
        for task_id in sorted(self.tasks):
            if self.tasks[task_id]["story_id"] in story_ids:
                await self._lock("task", task_id, held)
                rows.append({"task_id": task_id, "status": self.tasks[task_id]["status"]})
        return rows

    def _set_rows(self, kind: str, params: Mapping[str, Any], overlay: Overlay | None) -> list[dict[str, Any]]:
        """Write into the transaction's overlay (committed at its end), or straight to the store outside one."""

        committed = self.stories if kind == "stories" else self.tasks
        target = committed if overlay is None else overlay.setdefault(kind, {})
        seen = self._seen(kind, overlay)
        for row in params["rows"]:
            target[row["id"]] = _merge(seen[row["id"]], {**row["properties"], "updated_at": params["now"]})
        return [{"written": len(params["rows"])}]

    async def _claim_task(self, params: Mapping[str, Any], held: list[asyncio.Lock]) -> list[dict[str, Any]]:
        task_id = params["task_id"]
        if task_id not in self.tasks:
            return []
        await self._lock("task", task_id, held)
        task = self.tasks[task_id]
        story = self.stories.get(task["story_id"])
        area = self.areas.get(task["area_id"])
        waits = [self.tasks[target]["status"] for source, target in self.waits if source == task_id]
        unready = (
            area is None
            or area["status"] != "active"
            or story is None
            or story["status"] != "active"
            or any(status != "completed" for status in waits)
        )
        if self.plan is not None and self.plan.get("applying"):
            outcome = "plan_applying"
        elif task["status"] != "pending":
            outcome = "wrong_state"
        elif story is not None and story.get("claimed_by"):
            outcome = "story_claimed"
        else:
            outcome = "not_ready" if unready else "ok"
        if outcome == "ok":
            self.tasks[task_id] = _merge(
                task,
                {
                    "status": "in_progress",
                    "claim_token": params["claim_token"],
                    "claimed_by": params["claimed_by"],
                    "claimed_at": params["now"],
                    "attempts": (task.get("attempts") or 0) + 1,
                    "updated_at": params["now"],
                },
            )
        return [
            {
                "outcome": outcome,
                "status": self.tasks[task_id]["status"],
                "task": copy.deepcopy(self.tasks[task_id]),
                "holder": story.get("claimed_by") if story else None,
                "held_at": story.get("claimed_at") if story else None,
            }
        ]

    async def _transition(self, params: Mapping[str, Any], held: list[asyncio.Lock]) -> list[dict[str, Any]]:
        task_id = params["task_id"]
        if task_id not in self.tasks:
            return []
        await self._lock("task", task_id, held)
        task = self.tasks[task_id]
        if task["status"] not in params["from_statuses"]:
            outcome = "wrong_state"
        elif (story := self.stories.get(task["story_id"])) is not None and story.get("claimed_by"):
            outcome = "story_claimed"
        elif params["check_token"] and (task.get("claim_token") or "") != params["claim_token"]:
            outcome = "claim_mismatch"
        else:
            outcome = "ok"
            self.tasks[task_id] = _merge(task, {**params["properties"], "updated_at": params["now"]})
        story = self.stories.get(task["story_id"]) or {}
        return [
            {
                "outcome": outcome,
                "status": self.tasks[task_id]["status"],
                "task": self.tasks[task_id],
                "holder": story.get("claimed_by"),
                "held_at": story.get("claimed_at"),
            }
        ]


class _Transaction:
    """One explicit transaction: its locks are held to the end, its writes stay its own until it commits."""

    def __init__(self, executor: MemoryPlanExecutor) -> None:
        self._executor = executor
        self._overlay: Overlay = {}
        self._held: list[asyncio.Lock] = []

    async def run(self, query: Query, /, **params: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        if query.name.startswith("plan_read_"):
            return self._executor._answer(query, self._overlay)
        return await self._executor._run(query, params, self._overlay, self._held)

    def commit(self) -> None:
        self._executor.stories.update(self._overlay.get("stories", {}))
        self._executor.tasks.update(self._overlay.get("tasks", {}))

    def release(self) -> None:
        for lock in self._held:
            lock.release()
        self._held.clear()


def _merge(row: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    """``SET row += values``: a null removes the property."""

    merged = {**row, **values}
    for key, value in values.items():
        if value is None:
            merged[key] = None
    return merged


def mini_plan(project_id: str = "shop") -> MemoryPlanExecutor:
    """The plan the story claim tests share.

    ``AREA::P`` (project) holds ``STORY::P::SETUP`` (``WS`` then ``TC``, both completed by ``ann`` at ``C0``).
    ``AREA::A`` holds ``STORY::A::API`` (release R1: ``CONTRACT`` then ``LOGIC``, after ``TC``), ``STORY::A::UI`` (R1:
    ``SCREEN``, after ``API_CONTRACT``), ``STORY::A::X`` and ``STORY::A::Y`` (a cycle: each logic waits for the other's
    contract; both contracts wait for ``TC``), ``STORY::A::LATE`` (release R2, after ``UI_SCREEN``) and
    ``STORY::A::STUCK`` (one ``blocked`` task, ``blocked_by`` ``FND::1``, one ``pending``)."""

    memory = MemoryPlanExecutor(project_id)
    memory.areas = {"AREA::P": area_row("AREA::P", "project"), "AREA::A": area_row("AREA::A")}
    stories = {
        "STORY::P::SETUP": {"kind": "setup", "pr_url": "https://git.example.test/p/1"},
        "STORY::A::API": {"release": "R1"},
        "STORY::A::UI": {"release": "R1"},
        "STORY::A::X": {},
        "STORY::A::Y": {},
        "STORY::A::LATE": {"release": "R2"},
        "STORY::A::STUCK": {},
    }
    memory.stories = {story_id: story_row(story_id, **values) for story_id, values in stories.items()}
    done = {"commit": "c" * 40, "completed_by": "ann", "completed_at": "2026-01-01T11:00:00+00:00"}
    tasks = [
        task_row("TASK::P::WS", "STORY::P::SETUP", "completed", **done),
        task_row(
            "TASK::P::TC", "STORY::P::SETUP", "completed", **{**done, "completed_at": "2026-01-01T12:00:00+00:00"}
        ),
        task_row("TASK::A::API_CONTRACT", "STORY::A::API", phase="contract", complexity="high"),
        task_row("TASK::A::API_LOGIC", "STORY::A::API"),
        task_row("TASK::A::UI_SCREEN", "STORY::A::UI"),
        task_row("TASK::A::X_CONTRACT", "STORY::A::X", phase="contract"),
        task_row("TASK::A::X_LOGIC", "STORY::A::X"),
        task_row("TASK::A::Y_CONTRACT", "STORY::A::Y", phase="contract"),
        task_row("TASK::A::Y_LOGIC", "STORY::A::Y"),
        task_row("TASK::A::LATE_ONE", "STORY::A::LATE"),
        task_row(
            "TASK::A::STUCK_BLOCKED", "STORY::A::STUCK", "blocked", block_reason="spec_missing", blocked_by="FND::1"
        ),
        task_row("TASK::A::STUCK_OPEN", "STORY::A::STUCK"),
    ]
    memory.tasks = {row["task_id"]: row for row in tasks}
    memory.add_waits(
        ("TASK::P::TC", "TASK::P::WS"),
        ("TASK::A::API_CONTRACT", "TASK::P::TC"),
        ("TASK::A::API_LOGIC", "TASK::A::API_CONTRACT"),
        ("TASK::A::UI_SCREEN", "TASK::A::API_CONTRACT"),
        ("TASK::A::X_CONTRACT", "TASK::P::TC"),
        ("TASK::A::Y_CONTRACT", "TASK::P::TC"),
        ("TASK::A::X_LOGIC", "TASK::A::Y_CONTRACT"),
        ("TASK::A::Y_LOGIC", "TASK::A::X_CONTRACT"),
        ("TASK::A::LATE_ONE", "TASK::A::UI_SCREEN"),
        ("TASK::A::STUCK_OPEN", "TASK::P::TC"),
    )
    return memory
