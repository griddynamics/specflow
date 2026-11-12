"""``Findings``: the findings of one database ([gap.md](gap.md) "Library: Findings").

Findings are foreign nodes with origin ``agent`` beside the spec graph. The library never reads a build's input, never
writes a spec node, edge, document or fragment, and never fetches a ``refers_to`` value. Finding text, notes and
reference values are stored, never logged: events carry ids, counts and codes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from graph.core.errors import ConfigurationError
from graph.core.identity import validate_project_id
from graph.core.ontology import Ontology
from graph.core.ontology.compiled import BUILD_OWNER
from graph.core.queries import Query
from graph.core.store import Executor, as_executor
from graph.core.writer import NodeRef, OwnedWriter
from graph.gap import arguments as check
from graph.gap.errors import FindingError
from graph.gap.models import (
    AddedFinding,
    Finding,
    FindingPage,
    FindingRef,
    FindingRefresh,
)
from graph.gap.names import (
    ABOUT,
    AGENT,
    EXTERNAL_REF,
    FINDING,
    OPEN_LIMIT,
    OWNER,
    REFERS_TO,
    STATUSES,
)
from graph.gap.queries import (
    DELETE_OPEN_FINDING,
    DELETE_UNUSED_REFS,
    READ_FINDINGS,
    SET_OPEN_FINDING,
)
from graph.gap.resolve import SpecRef, resolve_spec_ids
from graph.gap.rows import (
    about_edge_key,
    decode_after,
    encode_after,
    finding_uuid,
    new_finding_id,
    position,
    ref_uuid,
    refers_edge_key,
    sorted_findings,
    to_finding,
)
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.gap")

ID_ATTEMPTS = 5


class Findings:
    """Findings of every project of one graph database; ``clock`` gives UTC times (tests inject one)."""

    def __init__(
        self,
        store_or_executor: Any,
        ontology: Ontology,
        *,
        database: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = store_or_executor
        self._database = database
        self._executor: Executor = as_executor(store_or_executor, database)
        self._ontology = ontology
        self._labels = tuple(label for label in ontology.labels if ontology.owner_of(label) == BUILD_OWNER)
        self._clock = clock or (lambda: datetime.now(UTC))

    # Writes

    async def add(
        self,
        project_id: str,
        *,
        created_by: str,
        kind: str,
        text: str,
        about: Iterable[str],
        refers_to: Iterable[FindingRef | tuple[str, str]] = (),
        story_id: str | None = None,
        blocking: bool = False,
    ) -> AddedFinding:
        """Write an open finding, or return the open one with the same dedupe tuple (``created`` false, nothing
        written). Limits are checked before any write."""

        project_id = validate_project_id(project_id)
        check.check_author(created_by)
        check.check_kind(kind, blocking=blocking)
        check.check_text(text)
        check.check_story(story_id, blocking=blocking)
        asked = check.about_ids(about)
        refs = check.references(refers_to)
        current, key, nodes = await self._tuple(project_id, created_by, kind, asked, text, strict=True)
        existing = await self._read(project_id, statuses=("open",), dedupe_key=key)
        if existing:
            return AddedFinding(finding=existing[0], created=False)
        mine = await self._read(project_id, statuses=("open",), created_by=created_by)
        if sum(1 for item in mine if item.story_id == story_id) >= OPEN_LIMIT:
            raise FindingError("finding_limit", limit=OPEN_LIMIT)
        finding_id = await self._free_id(project_id)
        now = self._now()
        props: dict[str, Any] = {
            "finding_id": finding_id,
            "kind": kind,
            "text": text,
            "origin": AGENT,
            "status": "open",
            "blocking": blocking,
            "about_spec_ids": list(current),
            "created_by": created_by,
            "created_at": now,
            "orphaned": False,
            "dedupe_key": key,
            "updated_at": now,
            **({} if story_id is None else {"story_id": story_id}),
        }
        owner_uuid = finding_uuid(project_id, finding_id)
        async with self._writer(project_id).transaction() as writer:
            await writer.upsert_node(FINDING, owner_uuid, props)
            for ref in refs:
                node = ref_uuid(project_id, ref)
                await writer.upsert_node(EXTERNAL_REF, node, {"type": ref.type, "value": ref.value})
                await writer.upsert_relationship(
                    REFERS_TO,
                    NodeRef(FINDING, owner_uuid),
                    NodeRef(EXTERNAL_REF, node),
                    refers_edge_key(finding_id, node),
                    {},
                )
            for spec_id in current:
                ref_node = nodes[spec_id]
                await writer.upsert_relationship(
                    ABOUT,
                    NodeRef(FINDING, owner_uuid),
                    NodeRef(ref_node.label, ref_node.uuid),
                    about_edge_key(finding_id, spec_id),
                    {},
                )
        log_event(
            logger,
            INFO,
            "graph.gap.finding_added",
            finding_id=finding_id,
            kind=kind,
            about=len(current),
            refs=len(refs),
            blocking=blocking,
        )
        return AddedFinding(finding=await self._one(project_id, finding_id), created=True)

    async def match(
        self,
        project_id: str,
        *,
        created_by: str,
        kind: str,
        about: Iterable[str],
        text: str,
    ) -> Finding | None:
        """The open finding with the same dedupe tuple, if any."""

        project_id = validate_project_id(project_id)
        check.check_author(created_by)
        check.check_kind(kind)
        check.check_text(text)
        asked = check.about_ids(about)
        _, key, _ = await self._tuple(project_id, created_by, kind, asked, text, strict=False)
        found = await self._read(project_id, statuses=("open",), dedupe_key=key)
        return found[0] if found else None

    async def withdraw(self, project_id: str, finding_id: str) -> bool:
        """Delete an open finding with its edges and the ``ExternalRef`` nodes no other finding cites; host
        compensation only. False when it is gone or closed."""

        project_id = validate_project_id(project_id)
        found = await self._read(project_id, statuses=("open",), finding_id=finding_id)
        if not found:
            return False
        refs = [ref_uuid(project_id, ref) for ref in found[0].refers_to]
        async with self._executor.tx(project_id) as tx:
            rows = await tx.run(DELETE_OPEN_FINDING, finding_id=finding_id)
            deleted = bool(rows and rows[0]["deleted"])
            if deleted and refs:
                await tx.run(DELETE_UNUSED_REFS, uuids=refs)
        if deleted:
            log_event(logger, INFO, "graph.gap.finding_withdrawn", finding_id=finding_id)
        return deleted

    async def resolve(self, project_id: str, finding_id: str, *, by: str, note: str | None = None) -> Finding:
        """Close an open finding as ``resolved`` (a person's call), clearing its dedupe key."""

        return await self._close(project_id, finding_id, "resolved", by, note)

    async def dismiss(self, project_id: str, finding_id: str, *, by: str, note: str | None = None) -> Finding:
        """Close an open finding as ``dismissed``."""

        return await self._close(project_id, finding_id, "dismissed", by, note)

    # Reads

    async def finding(self, project_id: str, finding_id: str) -> Finding | None:
        found = await self._read(validate_project_id(project_id), statuses=STATUSES, finding_id=finding_id)
        return found[0] if found else None

    async def findings(
        self,
        project_id: str,
        *,
        status: str = "open",
        spec_ids: Sequence[str] | None = None,
        story_id: str | None = None,
        created_by: str | None = None,
        blocking: bool | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> FindingPage:
        """Blocking first, then newest first, then ``finding_id``; ``spec_ids`` match through aliases."""

        project_id = validate_project_id(project_id)
        check.check_limit(limit)
        if status != "all" and status not in STATUSES:
            raise ConfigurationError("invalid_status")
        start = None if after is None else decode_after(after)
        wanted = await self._asked(project_id, spec_ids) if spec_ids else ()
        found = await self._read(
            project_id,
            statuses=STATUSES if status == "all" else (status,),
            created_by=created_by,
            spec_ids=wanted,
        )
        kept = [
            item
            for item in sorted_findings(found)
            if (story_id is None or item.story_id == story_id)
            and (blocking is None or item.blocking == blocking)
            and (start is None or position(item) > start)
        ]
        page = kept[:limit]
        more = len(kept) > limit
        return FindingPage(
            findings=tuple(page),
            next=encode_after(position(page[-1])) if more else None,
        )

    async def open_counts(self, project_id: str, spec_ids: Sequence[str]) -> dict[str, int]:
        """Open findings per asked id (aliases resolve; keys as asked)."""

        project_id = validate_project_id(project_id)
        ids = tuple(dict.fromkeys(spec_ids))
        if not ids:
            return {}
        resolved = await resolve_spec_ids(self._executor, project_id, ids, self._labels)
        names = {spec_id: {spec_id, resolved[spec_id].spec_id} if spec_id in resolved else {spec_id} for spec_id in ids}
        everything = sorted(set().union(*names.values()))
        found = await self._read(project_id, statuses=("open",), spec_ids=everything)
        return {spec_id: sum(1 for item in found if names[spec_id] & set(item.about_spec_ids)) for spec_id in ids}

    async def refresh(self, project_id: str) -> FindingRefresh:
        """After a build: resolve each open finding's ids through the aliases, restore a missing ``ABOUT`` edge to a
        node that is back, and set ``orphaned`` to whether any id resolves to no node."""

        project_id = validate_project_id(project_id)
        open_findings = await self._read(project_id, statuses=("open",))
        every = tuple(dict.fromkeys(spec_id for item in open_findings for spec_id in item.about_spec_ids))
        resolved = await resolve_spec_ids(self._executor, project_id, every, self._labels)
        orphaned = 0
        for item in open_findings:
            orphaned += await self._refresh_one(project_id, item, resolved)
        log_event(
            logger,
            INFO,
            "graph.gap.findings_refreshed",
            refreshed=len(open_findings),
            orphaned=orphaned,
        )
        return FindingRefresh(refreshed=len(open_findings), orphaned=orphaned)

    # Plumbing

    async def _refresh_one(self, project_id: str, item: Finding, resolved: dict[str, SpecRef]) -> int:
        current = tuple(sorted({resolved[i].spec_id if i in resolved else i for i in item.about_spec_ids}))
        gone = any(spec_id not in resolved for spec_id in item.about_spec_ids)
        owner_uuid = finding_uuid(project_id, item.finding_id)
        if any(spec_id in resolved for spec_id in item.about_spec_ids):
            async with self._writer(project_id).transaction() as writer:
                for old in item.about_spec_ids:
                    if old in resolved and resolved[old].spec_id != old:
                        await writer.delete_relationship(ABOUT, about_edge_key(item.finding_id, old))
                for old in item.about_spec_ids:
                    if old in resolved:
                        node = resolved[old]
                        key = about_edge_key(item.finding_id, node.spec_id)
                        await writer.upsert_relationship(
                            ABOUT,
                            NodeRef(FINDING, owner_uuid),
                            NodeRef(node.label, node.uuid),
                            key,
                            {},
                        )
        if current != item.about_spec_ids or gone != item.orphaned:
            fresh = check.dedupe_key(item.created_by, item.kind, current, item.text)
            patch = {
                "about_spec_ids": list(current),
                "orphaned": gone,
                "dedupe_key": fresh,
            }
            await self._executor.write(
                SET_OPEN_FINDING,
                project_id,
                finding_id=item.finding_id,
                props=patch,
                now=self._now(),
            )
        return int(gone)

    async def _close(self, project_id: str, finding_id: str, status: str, by: str, note: str | None) -> Finding:
        project_id = validate_project_id(project_id)
        check.check_author(by)
        text = check.check_note(note)
        current = await self.finding(project_id, finding_id)
        if current is None:
            raise FindingError("unknown_finding")
        if current.status != "open":
            raise FindingError("wrong_state")
        now = self._now()
        patch: dict[str, Any] = {
            "status": status,
            "resolved_by": by,
            "resolved_at": now,
            "dedupe_key": None,
            **({} if text is None else {"resolution_note": text}),
        }
        rows = await self._executor.write(SET_OPEN_FINDING, project_id, finding_id=finding_id, props=patch, now=now)
        if not rows or not rows[0]["written"]:
            raise FindingError("wrong_state")
        log_event(
            logger,
            INFO,
            "graph.gap.finding_closed",
            finding_id=finding_id,
            status=status,
            noted=text is not None,
        )
        return await self._one(project_id, finding_id)

    async def _tuple(
        self,
        project_id: str,
        created_by: str,
        kind: str,
        asked: tuple[str, ...],
        text: str,
        *,
        strict: bool,
    ) -> tuple[tuple[str, ...], str, dict[str, SpecRef]]:
        """The current ids of ``asked`` (sorted), the dedupe key and the nodes by current id; ``strict``: an id naming
        no node is refused."""

        resolved = await resolve_spec_ids(self._executor, project_id, asked, self._labels)
        missing = [spec_id for spec_id in asked if spec_id not in resolved]
        if missing and strict:
            raise FindingError("node_not_found", spec_ids=missing)
        current = tuple(sorted({resolved[i].spec_id if i in resolved else i for i in asked}))
        nodes = {node.spec_id: node for node in resolved.values()}
        return current, check.dedupe_key(created_by, kind, current, text), nodes

    async def _asked(self, project_id: str, spec_ids: Sequence[str]) -> tuple[str, ...]:
        """The ids asked for and the current ids they resolve to."""

        ids = check.spec_id_filter(spec_ids)
        resolved = await resolve_spec_ids(self._executor, project_id, ids, self._labels)
        return tuple(sorted({*ids, *(ref.spec_id for ref in resolved.values())}))

    async def _free_id(self, project_id: str) -> str:
        for _ in range(ID_ATTEMPTS):
            candidate = new_finding_id()
            if not await self._read(project_id, statuses=STATUSES, finding_id=candidate):
                return candidate
        raise FindingError("finding_limit")

    async def _one(self, project_id: str, finding_id: str) -> Finding:
        found = await self._read(project_id, statuses=STATUSES, finding_id=finding_id)
        return found[0]

    async def _read(
        self,
        project_id: str,
        *,
        statuses: Sequence[str],
        finding_id: str | None = None,
        created_by: str | None = None,
        dedupe_key: str | None = None,
        spec_ids: Sequence[str] = (),
    ) -> list[Finding]:
        query: Query = READ_FINDINGS
        rows = await self._executor.read(
            query,
            project_id,
            statuses=list(statuses),
            finding_id=finding_id,
            created_by=created_by,
            dedupe_key=dedupe_key,
            spec_ids=list(spec_ids),
        )
        return [to_finding(row) for row in rows]

    def _writer(self, project_id: str) -> OwnedWriter:
        return OwnedWriter(self._source, self._ontology, OWNER, project_id, database=self._database)

    def _now(self) -> str:
        return self._clock().isoformat(timespec="milliseconds")
