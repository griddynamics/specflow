"""Change set, units and windows of the Plan step (build.md Plan 2 and 5; tech-specs 5.4 "Windows").

``change_set`` compares the snapshot with the persisted documents; ``match_fragments`` matches the
fragments of the snapshot to the persisted ones and lists the fragments that retire (build.md Plan 4);
``plan_units`` groups fragments into units and node-extraction windows; ``dirty_windows`` narrows a
unit's windows to its dirty fragments.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Final, get_args

from graph.build.models import ChangeSet, DocumentRec, FragmentRec, FragmentStatus, Unit, Window
from graph.build.options import BuildOptions
from graph.build.ports import CorpusRevision
from graph.build.queries import READ_PERSISTED_DOCUMENTS, READ_PERSISTED_FRAGMENTS, READ_PERSISTED_PRESENCE
from graph.core.db import Executor

# Cost of a unit cut per heading level below the top (build.md Plan 5 "preferring top-level
# headings"): a cut moves to a higher-level heading when that costs less imbalance, measured as the
# squared relative deviation of each part from the ideal size.
HEADING_DEPTH_PENALTY: Final = 0.2
UNIT_ID_PREFIX: Final = "unit:"
LINE_JACCARD: Final = 0.5
"""Line-set Jaccard at least this, under the same heading path, makes a fragment ``changed`` (tech-specs 5.4)."""
DIRTY_CHANGES: Final = frozenset({"new", "changed", "retried"})
FRAGMENT_STATUSES: Final = frozenset(get_args(FragmentStatus))


async def persisted_documents(executor: Executor, project_id: str) -> list[DocumentRec]:
    """Every persisted ``Document`` of the project (active and removed), ordered by path."""

    rows = await executor.read(READ_PERSISTED_DOCUMENTS, project_id)
    return [DocumentRec.model_validate(row) for row in rows]


async def has_persisted_graph(executor: Executor, project_id: str) -> bool:
    """Whether the project has any ``Document`` or spec node (an interrupted persist included)."""

    rows = await executor.read(READ_PERSISTED_PRESENCE, project_id)
    return bool(rows) and bool(rows[0].get("documents") or rows[0].get("spec_nodes"))


async def persisted_fragments(executor: Executor, project_id: str) -> list[FragmentRec]:
    """Every persisted ``Fragment`` of the project that is not ``retired``, by document and ordinal.

    The rows are fragments as they were persisted (``change`` left at its default); their ``tokens``
    are not stored and read as 0. A status outside the data model (a fault verification reports as
    ``fragment_state``) is read as ``active`` and never extracted (``extracted_hash`` None), so a
    retry extracts the fragment again and its persisted status is rewritten."""

    rows = await executor.read(READ_PERSISTED_FRAGMENTS, project_id)
    fragments = []
    for row in rows:
        values = {**row, "heading_path": tuple(row.get("heading_path") or ()), "tokens": 0}
        if values.get("status") not in FRAGMENT_STATUSES:
            values.update(status="active", extracted_hash=None)
        fragments.append(FragmentRec.model_validate(values))
    return fragments


def needs_retry(fragment: FragmentRec) -> bool:
    """A persisted fragment a retry extracts again: ``skipped``, or ``active`` without its current
    content extracted (``extracted_hash`` is not its ``content_hash``; verification's
    ``fragment_state``, which requests the retry)."""

    if fragment.status == "skipped":
        return True
    return fragment.status == "active" and fragment.extracted_hash != fragment.content_hash


def change_set(
    revisions: Sequence[CorpusRevision],
    persisted: Sequence[DocumentRec],
    retry_lineages: Collection[str] = (),
) -> ChangeSet:
    """Lineage ids by change (build.md Plan 2), each list in snapshot order (``removed``: persisted order).

    A lineage without an active persisted document is new; another ``revision_id`` is revised; an
    active document missing from the snapshot is removed. ``retried`` lists the requested lineages
    that are in the snapshot and unchanged (a new or revised lineage is extracted anyway).
    """

    active = {document.document_id: document for document in persisted if document.status == "active"}
    in_snapshot = {revision.lineage_id for revision in revisions}
    new: list[str] = []
    revised: list[str] = []
    unchanged: list[str] = []
    for revision in revisions:
        document = active.get(revision.lineage_id)
        if document is None:
            new.append(revision.lineage_id)
        elif document.revision_id != revision.revision_id:
            revised.append(revision.lineage_id)
        else:
            unchanged.append(revision.lineage_id)
    retry = set(retry_lineages)
    return ChangeSet(
        new=tuple(new),
        revised=tuple(revised),
        unchanged=tuple(unchanged),
        removed=tuple(document_id for document_id in active if document_id not in in_snapshot),
        retried=tuple(lineage for lineage in unchanged if lineage in retry),
    )


def _line_set(text: str) -> frozenset[str]:
    return frozenset(line.strip() for line in text.split("\n") if line.strip())


def line_jaccard(first: str, second: str) -> float:
    """Jaccard of the sets of non-blank stripped lines of two texts (0.0 when both are empty)."""

    a, b = _line_set(first), _line_set(second)
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _pair_document(current: Sequence[FragmentRec], old: Sequence[FragmentRec]) -> dict[int, tuple[FragmentRec, str]]:
    """One to one, the current fragments of a document (by index) paired with persisted ones, with the
    ``change`` the pair means: same id and hash, then same hash (``unchanged``), then same heading path
    with a line-set Jaccard of at least ``LINE_JACCARD`` (``changed``)."""

    pairs: dict[int, tuple[FragmentRec, str]] = {}
    free = set(range(len(old)))
    position = {fragment.fragment_id: index for index, fragment in enumerate(old)}
    for i, fragment in enumerate(current):
        index = position.get(fragment.fragment_id)
        if index is not None and index in free and old[index].content_hash == fragment.content_hash:
            pairs[i] = (old[index], "unchanged")
            free.discard(index)
    for i, fragment in enumerate(current):
        if i in pairs:
            continue
        index = next((j for j in sorted(free) if old[j].content_hash == fragment.content_hash), None)
        if index is not None:
            pairs[i] = (old[index], "unchanged")
            free.discard(index)
    candidates = sorted(
        (-score, i, j)
        for i, fragment in enumerate(current)
        if i not in pairs
        for j in free
        if old[j].heading_path == fragment.heading_path
        and (score := line_jaccard(old[j].text, fragment.text)) >= LINE_JACCARD
    )
    for _, i, j in candidates:
        if i not in pairs and j in free:
            pairs[i] = (old[j], "changed")
            free.discard(j)
    return pairs


def match_fragments(
    fragments: Sequence[FragmentRec],
    persisted: Sequence[FragmentRec],
    retry_lineages: Collection[str] = (),
) -> tuple[list[FragmentRec], tuple[str, ...]]:
    """The snapshot's fragments with their ``change`` against the persisted fragments, and the ids of
    the persisted fragments that retire (build.md Plan 4; FR-BLD-04, FR-BLD-05).

    Per document, one to one, in this order: the same id and content hash, then the same content
    hash (document order) → ``unchanged``; then the same heading path with a line-set Jaccard of at
    least ``LINE_JACCARD`` (best first, ties by ordinal) → ``changed`` with ``previous_id``; the rest
    are ``new``. An unchanged fragment keeps the persisted ``status``, ``extracted_hash`` and
    ``error_code``; one in a lineage of ``retry_lineages`` that ``needs_retry`` (stored ``skipped``, or
    ``active`` without its content extracted) becomes ``retried``. A
    changed fragment takes the persisted ``extracted_hash`` (its last extracted version, FR-BLD-12).
    ``previous_id`` is set whenever the match has another id.

    Retired: persisted fragments without a match (every fragment of a removed document among them),
    and matched ones whose id no current fragment carries any more; in persisted order.
    """

    retry = set(retry_lineages)
    by_document: dict[str, list[FragmentRec]] = {}
    for fragment in persisted:
        by_document.setdefault(fragment.document_id, []).append(fragment)
    result: list[FragmentRec] = []
    for document_id, group in itertools.groupby(fragments, key=lambda fragment: fragment.document_id):
        current = list(group)
        pairs = _pair_document(current, by_document.get(document_id, []))
        for i, fragment in enumerate(current):
            match = pairs.get(i)
            if match is None:
                result.append(fragment.model_copy(update={"change": "new"}))
                continue
            previous, change = match
            update: dict[str, object] = {
                "change": change,
                "previous_id": previous.fragment_id if previous.fragment_id != fragment.fragment_id else None,
                "extracted_hash": previous.extracted_hash,
            }
            if change == "unchanged":
                update.update(status=previous.status, error_code=previous.error_code)
                if document_id in retry and needs_retry(previous):
                    update["change"] = "retried"
            result.append(fragment.model_copy(update=update))
    current_ids = {fragment.fragment_id for fragment in result}
    return result, tuple(fragment.fragment_id for fragment in persisted if fragment.fragment_id not in current_ids)


def dirty_ids(fragments: Sequence[FragmentRec], *, everything: bool, stale: Collection[str] = ()) -> tuple[str, ...]:
    """Fragments to extract, in document order: every fragment with ``everything`` (``first`` and
    ``rebuild``), else the new, changed and retried ones and those in ``stale`` (an unchanged fragment
    whose stored nodes no longer match the extractor fingerprint)."""

    return tuple(
        fragment.fragment_id
        for fragment in fragments
        if everything or fragment.change in DIRTY_CHANGES or fragment.fragment_id in stale
    )


def plan_units(fragments: Sequence[FragmentRec], options: BuildOptions) -> list[Unit]:
    """Units of consecutive fragments of one document, each with windows over all its fragments.

    A document within ``unit_tokens`` is one unit; a longer one is cut into the fewest balanced parts
    within ``unit_tokens`` at fragment boundaries, preferring top-level headings. Each unit's windows
    are a balanced partition by tokens into ``ceil(tokens / window_tokens(options))`` windows, at most
    ``max_node_windows``. A fragment larger than ``unit_tokens`` (an unsplittable code block) is a unit
    of its own; its extraction call overflows or is truncated and its fragment is skipped (build.md
    "Failures").
    """

    units: list[Unit] = []
    for _, group in itertools.groupby(fragments, key=lambda fragment: fragment.document_id):
        for members in _around_oversize(list(group), options.unit_tokens):
            units.extend(_unit(members[start:end], options) for start, end in _unit_runs(members, options))
    return units


def _around_oversize(members: list[FragmentRec], limit: int) -> list[list[FragmentRec]]:
    """``members`` cut around every fragment over ``limit``, which stands alone."""

    parts: list[list[FragmentRec]] = [[]]
    for fragment in members:
        if fragment.tokens > limit:
            parts.extend(([fragment], []))
        else:
            parts[-1].append(fragment)
    return [part for part in parts if part]


def _unit_runs(members: Sequence[FragmentRec], options: BuildOptions) -> list[Window]:
    """The fewest balanced runs within ``unit_tokens``; one run per fragment at worst, which always fits."""

    sizes = [fragment.tokens for fragment in members]
    penalties = _cut_penalties(members)
    parts = min(len(members), max(1, -(-sum(sizes) // options.unit_tokens)))
    runs = _partition(sizes, parts, options.unit_tokens, penalties)
    while runs is None and parts < len(members):
        parts += 1
        runs = _partition(sizes, parts, options.unit_tokens, penalties)
    return runs if runs is not None else [(index, index + 1) for index in range(len(members))]


def window_tokens(options: BuildOptions) -> int:
    """Token budget of one node-extraction window: ``ceil(unit_tokens / max_node_windows)``."""

    return -(-options.unit_tokens // options.max_node_windows)


def dirty_windows(
    unit: Unit, dirty_ids: Collection[str], *, max_windows: int, tokens: Mapping[str, int]
) -> list[Window]:
    """Windows over the unit's dirty fragments: one per maximal run of dirty fragments, each run cut
    again where the unit's planned windows (``unit.windows``) start, so a fully dirty unit keeps its
    planned windows. Empty when nothing in the unit is dirty.

    Never more than ``max_windows``: while there are more, the adjacent pair whose merged window has
    the fewest tokens (``tokens`` by fragment id; earliest on ties) is merged. A merged window also
    covers the clean fragments between the two runs.
    """

    starts = {start for start, _ in unit.windows}
    windows: list[Window] = []
    run_start: int | None = None
    for index, fragment_id in enumerate((*unit.fragment_ids, None)):
        dirty = fragment_id is not None and fragment_id in dirty_ids
        if run_start is not None and (not dirty or index in starts):
            windows.append((run_start, index))
            run_start = None
        if dirty and run_start is None:
            run_start = index
    if len(windows) > max_windows:
        prefix = list(itertools.accumulate((tokens.get(fid, 0) for fid in unit.fragment_ids), initial=0))
        while len(windows) > max(1, max_windows):
            merged = min(
                range(len(windows) - 1),
                key=lambda i: (prefix[windows[i + 1][1]] - prefix[windows[i][0]], i),
            )
            windows[merged : merged + 2] = [(windows[merged][0], windows[merged + 1][1])]
    return windows


def _unit(members: Sequence[FragmentRec], options: BuildOptions) -> Unit:
    tokens = sum(fragment.tokens for fragment in members)
    count = min(options.max_node_windows, len(members), max(1, -(-tokens // window_tokens(options))))
    sizes = [fragment.tokens for fragment in members]
    windows = _partition(sizes, count, None, [0.0] * len(members)) or [(0, len(members))]
    digest = hashlib.sha256("\n".join(fragment.content_hash for fragment in members).encode("utf-8"))
    return Unit(
        unit_id=f"{UNIT_ID_PREFIX}{members[0].fragment_id}",
        document_id=members[0].document_id,
        fragment_ids=tuple(fragment.fragment_id for fragment in members),
        tokens=tokens,
        content_hash=digest.hexdigest(),
        windows=tuple(windows),
    )


def _cut_penalties(members: Sequence[FragmentRec]) -> list[float]:
    """Penalty of a cut before each fragment: deeper shared heading path, higher penalty."""

    penalties = [0.0]
    for previous, current in itertools.pairwise(members):
        shared = 0
        for left, right in zip(previous.heading_path, current.heading_path, strict=False):
            if left != right:
                break
            shared += 1
        penalties.append(HEADING_DEPTH_PENALTY * shared)
    return penalties


def _cheapest_cuts(
    count: int, parts: int, run_cost: Callable[[int, int], float | None], penalties: Sequence[float]
) -> tuple[list[list[float]], list[list[int]]]:
    """The cost table of ``_partition``: ``best[k][j]`` is the least cost of cutting the first ``j`` sizes
    into ``k`` runs and ``choice[k][j]`` the start of the last run."""

    infinity = float("inf")
    best = [[infinity] * (count + 1) for _ in range(parts + 1)]
    choice = [[0] * (count + 1) for _ in range(parts + 1)]
    best[0][0] = 0.0
    for k in range(1, parts + 1):
        for j in range(k, count - (parts - k) + 1):
            for i in range(k - 1, j):
                if best[k - 1][i] == infinity:
                    continue
                cost = run_cost(i, j)
                if cost is None:
                    continue
                total = best[k - 1][i] + cost + (penalties[i] if i > 0 else 0.0)
                if total < best[k][j]:
                    best[k][j] = total
                    choice[k][j] = i
    return best, choice


def _partition(sizes: Sequence[int], parts: int, limit: int | None, penalties: Sequence[float]) -> list[Window] | None:
    """Cut ``sizes`` into ``parts`` contiguous non-empty runs, minimising the squared relative deviation
    from the mean plus the cut penalties; a run over ``limit`` is allowed only as a single item.
    None when no such cut exists. Ties keep the earliest cut, so the result is deterministic."""

    count = len(sizes)
    if parts < 1 or parts > count:
        return None if count else []
    ideal = max(sum(sizes) / parts, 1.0)
    prefix = list(itertools.accumulate(sizes, initial=0))

    def run_cost(start: int, end: int) -> float | None:
        size = prefix[end] - prefix[start]
        if limit is not None and size > limit and end - start > 1:
            return None
        return ((size - ideal) / ideal) ** 2

    infinity = float("inf")
    best, choice = _cheapest_cuts(count, parts, run_cost, penalties)
    if best[parts][count] == infinity:
        return None
    windows: list[Window] = []
    end = count
    for k in range(parts, 0, -1):
        start = choice[k][end]
        windows.append((start, end))
        end = start
    return windows[::-1]
