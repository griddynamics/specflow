"""The Linear objects the issues hang on: the owned labels (``graphico``, ``graphico-removed``), the area labels in the
single-select group "Graphico area", and the project's release milestones. Each is found by name first (a person may
have made it, and a team's labels are shared by every Graphico project of the team) and made with a client UUID
(``linear.ids``) when missing, so a retried create adopts what is already there."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import suppress

from api.linear_plan.content import AREA_GROUP, OWNED_LABEL, REMOVED_LABEL
from linear.errors import LinearAlreadyExistsError
from linear.gateway import LinearGateway
from linear.ids import KIND_LABEL, KIND_MILESTONE, node_uuid
from linear.models import Label

_OWNED_NAMES = frozenset({OWNED_LABEL, REMOVED_LABEL})


def _same(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


class Scaffold:
    """Labels of one team and milestones of one Linear project, for one pass."""

    def __init__(self, gateway: LinearGateway, project_id: uuid.UUID, *, team_id: str, linear_project_id: str) -> None:
        self._gateway = gateway
        self._project = project_id
        self._team = team_id
        self._linear_project = linear_project_id
        self._labels: list[Label] | None = None
        self._milestones: set[str] = set()

    async def _all(self, *, refresh: bool = False) -> list[Label]:
        if self._labels is None or refresh:
            self._labels = list(await self._gateway.labels(self._team))
        return self._labels

    def _find(self, labels: Sequence[Label], name: str, parent_id: str | None) -> Label | None:
        return next((x for x in labels if _same(x.name, name) and x.parent_id == parent_id), None)

    async def _ensure(self, name: str, *, parent_id: str | None = None, is_group: bool = False) -> Label:
        found = self._find(await self._all(), name, parent_id)
        if found is not None:
            return found
        label_id = str(node_uuid(self._project, KIND_LABEL, f"{parent_id or ''}/{name}"))
        try:
            made = await self._gateway.create_label(self._team, label_id, name, parent_id=parent_id, is_group=is_group)
        except LinearAlreadyExistsError:  # a person made it meanwhile, or an earlier pass did before it crashed
            found = self._find(await self._all(refresh=True), name, parent_id)
            if found is None:
                raise
            return found
        (await self._all()).append(made)
        return made

    async def label_ids(self, names: Sequence[str]) -> list[str]:
        """The label ids of the owned names and of the area labels (made under the group when missing)."""

        ids: list[str] = []
        group: Label | None = None
        for name in names:
            if name in _OWNED_NAMES:
                ids.append((await self._ensure(name)).id)
                continue
            group = group or await self._ensure(AREA_GROUP, is_group=True)
            ids.append((await self._ensure(name, parent_id=group.id)).id)
        return ids

    async def removed_label_id(self) -> str:
        return (await self._ensure(REMOVED_LABEL)).id

    async def is_owned(self, label: Label) -> bool:
        """True for a label a pass sets (and may therefore replace): the owned names and the area labels."""

        if label.name.casefold() in _OWNED_NAMES and label.parent_id is None:
            return True
        group = self._find(await self._all(), AREA_GROUP, None)
        return group is not None and label.parent_id == group.id

    async def milestone(self, release: str) -> str:
        """The milestone of a release (made on first use; its id is known, so an existing one is adopted)."""

        milestone_id = str(node_uuid(self._project, KIND_MILESTONE, release))
        if milestone_id not in self._milestones:
            with suppress(LinearAlreadyExistsError):  # an earlier pass made it: its id is known
                await self._gateway.create_milestone(self._linear_project, milestone_id, release)
            self._milestones.add(milestone_id)
        return milestone_id
