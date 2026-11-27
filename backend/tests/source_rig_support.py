"""The wiring the Box and Dropbox source route tests share (no tests in here): the source card of the import just
added, and ben (collaborator) and carol (no member) next to alice, who owns ``Alpha``."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from atlassian_import_support import SOURCE_ID, FakeImports, source_card
from db import source_files
from specflow_api_agent_support import ALPHA


async def wire_source_rig(monkeypatch: Any, served: Any, kind: str, imports: FakeImports) -> tuple[uuid.UUID, Any]:
    async def source_summary(_session: Any, _project: uuid.UUID, source_id: uuid.UUID) -> Any:
        added = imports.added[-1] if imports.added else {"name": "", "link": ""}
        card = source_card(kind, served.users["alice"].id, added["name"], added["link"])
        return card if source_id == SOURCE_ID else None

    monkeypatch.setattr(source_files, "source_summary", source_summary)
    repo = served.repo
    ben = await repo.upsert_user(
        None, SimpleNamespace(subject="user:ben@example.com", email="ben@example.com", name=None)
    )
    await repo.upsert_user(
        None, SimpleNamespace(subject="user:carol@example.com", email="carol@example.com", name=None)
    )
    alpha = uuid.UUID(ALPHA)
    repo.members.add((alpha, ben.id))
    repo.roles[(alpha, ben.id)] = "collaborator"
    return alpha, ben
