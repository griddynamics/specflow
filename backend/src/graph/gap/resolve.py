"""Spec ids to the current spec nodes: own ids and aliases, through the core queries only."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from graph.core.queries import READ_NODE_UUIDS_BY_SPEC_ID, READ_NODES_BY_UUID
from graph.core.store import Executor

RETIRED: Final = "retired"


@dataclass(frozen=True, slots=True)
class SpecRef:
    """The current node a spec id names: its storage key, current ``spec_id`` and domain label."""

    uuid: str
    spec_id: str
    label: str


async def resolve_spec_ids(
    executor: Executor, project_id: str, spec_ids: Sequence[str], labels: Sequence[str]
) -> dict[str, SpecRef]:
    """Each id of ``spec_ids`` that is the ``spec_id`` or an alias of a node of ``labels``, mapped to that node."""

    wanted = list(dict.fromkeys(spec_ids))
    if not wanted:
        return {}
    rows = await executor.read(READ_NODE_UUIDS_BY_SPEC_ID, project_id, spec_ids=wanted, labels=list(labels))
    uuids = [row["uuid"] for row in rows]
    if not uuids:
        return {}
    nodes = await executor.read(READ_NODES_BY_UUID, project_id, uuids=uuids, retired=RETIRED)
    refs: list[tuple[SpecRef, tuple[str, ...]]] = []
    for row in nodes:
        props: dict[str, Any] = row["props"]
        label = next((name for name in row["labels"] if name in labels), None)
        if label is not None:
            refs.append(
                (
                    SpecRef(props["uuid"], props["spec_id"], label),
                    tuple(props.get("spec_id_aliases") or ()),
                )
            )
    found = {ref.spec_id: ref for ref, _ in refs if ref.spec_id in wanted}  # a current id wins over an alias
    for ref, aliases in refs:
        for alias in aliases:
            if alias in wanted:
                found.setdefault(alias, ref)
    return found
