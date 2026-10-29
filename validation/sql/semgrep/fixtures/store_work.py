"""Fixtures for semgrep/store_work.yml: positive and negative cases per rule, one section
per rule id.

`semgrep --test` applies no `paths:` scope, so the repository and caller rules both see
this file; their scoping is proved by a real scan over `fixtures/src/` instead."""

import difflib
import heapq
import math
import operator
from difflib import get_close_matches

import numpy

from src.repositories import orders as order_repo
from src.repositories.graph_catalog import fetch_shelf_products, fetch_shelf_stocked

# --- kit.store_work.repository-result-shaped-in-python ---


class ItemRepository:
    def __init__(self, db: object, graph: object) -> None:
        self._db = db
        self._gdb = graph

    async def active_items(self, statement: object) -> list[object]:
        result = await self._db.execute(statement)
        rows = result.scalars().all()
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        return [row for row in rows if row.status == "ACTIVE"]

    async def newest_first(self, statement: object) -> list[object]:
        rows = (await self._db.execute(statement)).scalars().all()
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        return sorted(rows, key=lambda row: row.created_at, reverse=True)

    async def latest(self, statement: object) -> object:
        result = await self._db.scalars(statement)
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        return max(result, key=lambda row: row.created_at)

    async def first_open(self, statement: object) -> object | None:
        result = await self._db.execute(statement)
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        return next((row for row in result.scalars() if row.open), None)

    async def ranked(self, query: object) -> list[dict[str, object]]:
        result = await self._gdb.run(query)
        rows: list[dict[str, object]] = []
        async for record in result:
            rows.append({"id": record["id"], "score": record["score"]})
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        rows.sort(key=lambda row: row["score"])
        return rows

    async def boosted(self, query: object) -> list[tuple[str, float]]:
        result = await self._gdb.run(query)
        scored = [(str(record["id"]), boost(record["score"])) async for record in result]
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        return heapq.nlargest(3, scored, key=lambda item: item[1])

    async def live_nodes(self, query: object) -> list[str]:
        result = await self._gdb.run(query)
        live: list[str] = []
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        async for record in result:
            if record["merged_into"] is not None:
                continue
            live.append(str(record["id"]))
        return live

    async def item_ids(self, statement: object) -> list[str]:
        result = await self._db.execute(statement)
        # ok: kit.store_work.repository-result-shaped-in-python
        return [str(row.id) for row in result.scalars().all()]

    async def tags(self, query: object) -> list[str]:
        result = await self._gdb.run(query)
        row = await result.single()
        # ok: kit.store_work.repository-result-shaped-in-python
        return [tag for tag in row.tags if tag]

    async def neighbours(self, query: object, follow: object) -> list[object]:
        result = await self._gdb.run(query)
        rows = [record async for record in result]
        # ok: kit.store_work.repository-result-shaped-in-python
        more = await self._gdb.run(follow, ids=sorted({row["id"] for row in rows}))
        return [rows, more]

    async def reachable(self, query: object, follow: object) -> list[object]:
        # The predicate spanning both stores: SQL answered for the graph rows' keys.
        result = await self._gdb.run(query)
        rows = [record async for record in result]
        reached = await self._db.execute(follow, {"ids": [row["id"] for row in rows]})
        # ok: kit.store_work.repository-result-shaped-in-python
        return [row for row in rows if row["id"] in reached]

    async def reached_without_keys(self, query: object, follow: object) -> list[object]:
        result = await self._gdb.run(query)
        rows = [record async for record in result]
        reached = await self._db.execute(follow)
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        return [row for row in rows if row["id"] in reached]

    async def same_store_intersection(self, query: object, follow: object) -> list[object]:
        # A graph read of the graph rows' keys is one statement with the first.
        result = await self._gdb.run(query)
        rows = [record async for record in result]
        reached = await self._gdb.run(follow, ids=[row["id"] for row in rows])
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        return [row for row in rows if row["id"] in reached]

    async def page_size(self, statement: object, limit: int) -> int:
        result = await self._db.execute(statement)
        count = len(result.all())
        # ok: kit.store_work.repository-result-shaped-in-python
        return min(count, limit)

    async def close_each(self, statement: object, close: object) -> int:
        closed = 0
        # ok: kit.store_work.repository-result-shaped-in-python
        for row in (await self._db.execute(statement)).scalars().all():
            outcome = await self._db.execute(close, {"id": row.id})
            if outcome.rowcount != 1:
                continue
            closed += 1
        return closed

    async def kinds(self, statement: object) -> list[str]:
        result = await self._db.execute(statement)
        kept: list[str] = []
        # ruleid: kit.store_work.repository-result-shaped-in-python, kit.store_work.caller-shapes-repository-read
        for item_id, kind in result.all():
            if kind != "SERVICE":
                continue
            kept.append(item_id)
        return kept

    async def parsed(self, statement: object) -> list[object]:
        result = await self._db.execute(statement)
        specs: list[object] = []
        # ok: kit.store_work.repository-result-shaped-in-python
        for row in result.scalars().all():
            spec = parse_spec(row.spec)
            if spec is None:
                continue
            specs.append(spec)
        return specs

    async def attachments(self, statement: object) -> list[object]:
        result = await self._db.execute(statement)
        items: list[object] = []
        # ok: kit.store_work.repository-result-shaped-in-python
        for row in result.scalars().all():
            for item in row.attachments:
                if item is None:
                    continue
                items.append(item)
        return items


def boost(score: float) -> float:
    return score


def parse_spec(spec: object) -> object | None:
    return spec


# --- kit.store_work.repository-record-projection-filtered ---


async def live_candidates(graph: object, query: object) -> list[object]:
    result = await graph.run(query)
    return [
        {"id": record["id"], "label": record["label"]}
        # ruleid: kit.store_work.repository-record-projection-filtered
        async for record in result
        if record["merged_into"] is None
    ]


async def reusable_ids(graph: object, query: object, model: str) -> set[str]:
    result = await graph.run(query)
    # ruleid: kit.store_work.repository-record-projection-filtered
    return {str(record["id"]) async for record in result if record["model"] == model}


async def related_ids(graph: object, query: object) -> list[str]:
    result = await graph.run(query)
    record = await result.single()
    # ok: kit.store_work.repository-record-projection-filtered
    return [str(related) for related in (record["related_ids"] or []) if related]


def summarise(rows: object) -> list[object]:
    return [rows]


# --- kit.store_work.repository-result-sliced ---


async def first_page(db: object, statement: object, limit: int) -> list[object]:
    rows = (await db.execute(statement)).scalars().all()
    # ruleid: kit.store_work.repository-result-sliced, kit.store_work.caller-slices-repository-read
    return rows[:limit]


async def ranked_tail(graph: object, query: object) -> list[object]:
    result = await graph.run(query)
    ranked = [record async for record in result]
    # ruleid: kit.store_work.repository-result-sliced, kit.store_work.caller-slices-repository-read
    return ranked[1:]


async def row_columns(db: object, statement: object, width: int) -> list[object]:
    result = await db.execute(statement)
    # ok: kit.store_work.repository-result-sliced
    return [row[width:] for row in result.all()]


def requested_page(ids: list[str], limit: int) -> list[str]:
    # ok: kit.store_work.repository-result-sliced
    return ids[:limit]


async def best_first(db: object, statement: object, limit: int) -> list[object]:
    rows = (await db.execute(statement)).scalars().all()
    # ruleid: kit.store_work.repository-result-sliced, kit.store_work.caller-slices-repository-read
    return sorted(rows, key=lambda row: row.score)[:limit]


# --- kit.store_work.caller-shapes-repository-read ---


class ImportService:
    def __init__(self, session: object) -> None:
        self._runs = ImportRunRepository(session)
        self._repo = CatalogRepository(session)

    async def manual_run(self, supplier_id: str) -> object | None:
        return next(
            (
                run
                # ruleid: kit.store_work.caller-shapes-repository-read
                for run in await self._runs.list_by_supplier(supplier_id)
                if run.trigger == "MANUAL"
            ),
            None,
        )

    async def bundles(self, category_ids: list[str]) -> list[object]:
        items = await self._repo.list_items(category_ids)
        # ruleid: kit.store_work.caller-shapes-repository-read
        return [item for item in items if item.kind == "BUNDLE"]

    async def item_ids(self, category_ids: list[str]) -> list[str]:
        items = await self._repo.list_items(category_ids)
        # ok: kit.store_work.caller-shapes-repository-read
        return [item.id for item in items]

    async def sorted_item_ids(self, category_ids: list[str]) -> list[str]:
        items = await self._repo.list_items(category_ids)
        # ruleid: kit.store_work.caller-shapes-repository-read
        return sorted(item.id for item in items)

    async def summary(self, category_ids: list[str]) -> list[object]:
        items = await self._repo.list_items(category_ids)
        totals = summarise(items)
        # ok: kit.store_work.caller-shapes-repository-read
        return sorted(totals)

    async def first_import(self, supplier_id: str) -> object:
        runs = await self._runs.list_by_supplier(supplier_id)
        # ruleid: kit.store_work.caller-shapes-repository-read
        return min(run.created_at for run in runs)

    async def service_summary(self, category_ids: list[str]) -> list[object]:
        items = await self._repo.list_items(category_ids)
        # ruleid: kit.store_work.caller-shapes-repository-read
        totals = summarise([item for item in items if item.kind == "SERVICE"])
        return list(totals)

    async def failures(self, category_ids: list[str]) -> list[str]:
        items = await self._repo.list_items(category_ids)
        failed: dict[str, str] = {}
        failed.update(inventory_errors(items))
        # ok: kit.store_work.caller-shapes-repository-read
        return sorted(failed)

    async def reported(self) -> list[object]:
        rows = await self._reporter.read()
        # ok: kit.store_work.caller-shapes-repository-read
        return [row for row in rows if row.open]

    async def live_entries(self, category_ids: list[str]) -> list[object]:
        # A SQL read of a SQL result's keys is one statement with the first.
        rows = await self._repo.list_entries(category_ids)
        live = await self._repo.entries_by_ids([row.id for row in rows], statuses=("ACTIVE",))
        live_ids = {row.id for row in live}
        # ruleid: kit.store_work.caller-shapes-repository-read
        return [row for row in rows if row.id in live_ids]


async def above_threshold(db: object, category_ids: list[str]) -> list[object]:
    # A value no read of these rows' keys produced is the statement's parameter.
    items = await CatalogRepository(db).list_items(category_ids)
    threshold = await load_threshold(db)
    # ruleid: kit.store_work.caller-shapes-repository-read
    return [item for item in items if item.score >= threshold]


async def offered_catalogue(graph: object, db: object, shelf_id: str) -> list[object]:
    # An answer the read never took these rows' keys for is a filter over them.
    shelved = await fetch_shelf_products(graph, shelf_id=shelf_id)
    screen = await shelf_screen(graph, db, shelf_id)
    offered = {product.product_id for product in screen.products}
    # ruleid: kit.store_work.caller-shapes-repository-read
    return [row for row in shelved if row["id"] in offered]


async def stocked_products(graph: object, shelf_id: str) -> list[object]:
    # A graph repository function's answer for graph rows' keys is one statement.
    products = await fetch_shelf_products(graph, shelf_id=shelf_id)
    stocked = await fetch_shelf_stocked(graph, [row["id"] for row in products])
    # ruleid: kit.store_work.caller-shapes-repository-read
    return [row for row in products if row["id"] in stocked]


async def shown_items(deps: object, category_ids: list[str]) -> list[object]:
    # The predicate spanning both stores, over the graph nodes a service read for the SQL
    # rows' keys.
    async with deps.reads() as unit:
        items = await CatalogRepository(unit.db).list_items(category_ids)
        ids = [node_id for row in items for node_id in row.node_ids]
        found = await CatalogService(unit.db, graph=unit.graph).linked_nodes(ids)
    # ok: kit.store_work.caller-shapes-repository-read
    return [row for row in items if all(node_id in found for node_id in row.node_ids)]


async def load_threshold(db: object) -> float:
    return float(str(db))


async def shelf_screen(graph: object, db: object, shelf_id: str) -> object:
    return (graph, db, shelf_id)


async def inline_repository(db: object, category_ids: list[str]) -> list[object]:
    items = await CatalogRepository(db).list_items(category_ids)
    # ruleid: kit.store_work.caller-shapes-repository-read
    return [item for item in items if item.status == "ACTIVE"]


async def answered_in_session(deps: object, category_ids: list[str]) -> list[object]:
    async with deps.reads() as unit:
        items = await CatalogRepository(unit.db).list_items(category_ids)
        current = await GraphCatalogRepository(unit.graph).materialized(items)
    # ok: kit.store_work.caller-shapes-repository-read
    return [row for row in items if row.id in current]


def inventory_errors(items: object) -> dict[str, str]:
    return {str(items): ""}


async def nearest(graph: GraphCatalogRepository, text: str) -> object:
    hits = await graph.search(text)
    # ruleid: kit.store_work.caller-shapes-repository-read
    return min(hits, key=lambda hit: hit.distance)


async def labelled(db: object, keys: list[str]) -> dict[str, str]:
    texts = await order_repo.get_item_texts(db, keys)
    # ruleid: kit.store_work.caller-shapes-repository-read
    return {key: text for key, text in texts.items() if text}


async def offered_product(graph: object, shelf_id: str, product_id: str) -> object | None:
    products = await fetch_shelf_products(graph, shelf_id=shelf_id)
    # ruleid: kit.store_work.caller-shapes-repository-read
    return next((row for row in products if row["id"] == product_id), None)


async def basket_rows(db: object, graph: object, order_id: str) -> list[object]:
    order = await order_repo.get_order(db, order_id)
    basket = build_basket(graph, order)
    # ok: kit.store_work.caller-shapes-repository-read
    return [row for row in basket if row.weight > 0]


async def materialized(db: object, graph: object, category_ids: list[str]) -> list[object]:
    items = await CatalogRepository(db).active_item_specs(category_ids)
    current = await GraphCatalogRepository(graph).materialized([item.id for item in items])
    # ok: kit.store_work.caller-shapes-repository-read
    return [item for item in items if item.id in current]


async def keyed_materialized(db: object, graph: object, category_ids: list[str]) -> list[object]:
    # The predicate spanning both stores, over the keys assigned before the graph read.
    items = await CatalogRepository(db).active_item_specs(category_ids)
    ids = [item.id for item in items]
    if ids:
        current = await GraphCatalogRepository(graph).materialized(ids)
        # ok: kit.store_work.caller-shapes-repository-read
        return [item for item in items if item.id in current]
    return []


async def keyed_elsewhere(db: object, graph: object, category_ids: list[str]) -> list[object]:
    # Keys the rows never gave answer nothing about them.
    items = await CatalogRepository(db).active_item_specs(category_ids)
    ids = list(category_ids)
    current = await GraphCatalogRepository(graph).materialized(ids)
    # ruleid: kit.store_work.caller-shapes-repository-read
    return [item for item in items if item.id in current]


async def bundle_categories(db: object, bundle_id: str) -> list[str]:
    bundle = await CatalogRepository(db).get_bundle(bundle_id)
    # ok: kit.store_work.caller-shapes-repository-read
    return sorted(bundle.category_ids or [])


# --- kit.store_work.caller-slices-repository-read ---


async def first_tickets(db: object, limit: int) -> list[object]:
    repository = TicketRepository(db)
    tickets = await repository.list_tickets()
    # ruleid: kit.store_work.caller-slices-repository-read
    return tickets[:limit]


async def preview(db: object, text: str) -> str:
    repository = TicketRepository(db)
    ticket = await repository.get_ticket(text)
    # ok: kit.store_work.caller-slices-repository-read
    return text[:80] + str(ticket)


async def first_open_tickets(db: object, limit: int) -> list[object]:
    repository = TicketRepository(db)
    tickets = await repository.list_tickets()
    # ruleid: kit.store_work.caller-slices-repository-read
    return [ticket for ticket in tickets if ticket.open][:limit]


# --- kit.store_work.hand-rolled-vector-maths ---


def dot(first: list[float], second: list[float]) -> float:
    # ruleid: kit.store_work.hand-rolled-vector-maths
    return sum(a * b for a, b in zip(first, second, strict=True))


def norm(vector: list[float]) -> float:
    # ruleid: kit.store_work.hand-rolled-vector-maths
    return math.hypot(*vector)


def squared(vector: list[float]) -> float:
    # ruleid: kit.store_work.hand-rolled-vector-maths
    return math.fsum(value**2 for value in vector)


def self_products(vector: list[float]) -> float:
    # ruleid: kit.store_work.hand-rolled-vector-maths
    return sum(value * value for value in vector)


def mapped_dot(first: list[float], second: list[float]) -> float:
    # ruleid: kit.store_work.hand-rolled-vector-maths
    return sum(map(operator.mul, first, second))


def weighted(weights: list[float], scores: list[float]) -> float:
    # ok: kit.store_work.hand-rolled-vector-maths
    return sum(weight * 2 for weight in weights) + len(scores)


def products(rows: object, columns: object) -> object:
    # ruleid: kit.store_work.hand-rolled-vector-maths
    return rows @ columns


def numpy_cosine(first: object, second: object) -> object:
    # ruleid: kit.store_work.hand-rolled-vector-maths
    return numpy.dot(first, second) / numpy.linalg.norm(first)


def total(values: list[float]) -> float:
    # ok: kit.store_work.hand-rolled-vector-maths
    return sum(value for value in values)


def side(width: float, height: float) -> float:
    # ok: kit.store_work.hand-rolled-vector-maths
    return math.hypot(width, height)


# --- kit.store_work.stdlib-text-similarity ---


def ratio(first: str, second: str) -> float:
    # ruleid: kit.store_work.stdlib-text-similarity
    return difflib.SequenceMatcher(None, first, second).ratio()


def close(label: str, labels: list[str]) -> list[str]:
    # ruleid: kit.store_work.stdlib-text-similarity
    return get_close_matches(label, labels)


def same(first: str, second: str) -> bool:
    # ok: kit.store_work.stdlib-text-similarity
    return first.casefold() == second.casefold()
