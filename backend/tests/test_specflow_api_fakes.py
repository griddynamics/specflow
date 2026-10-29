"""In-memory stand-in for ``db.repositories`` (no tests in here)."""

from __future__ import annotations

import itertools
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from db import repositories as repo

_TICK = itertools.count()


def _stamp() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=next(_TICK))


class FakeSession:
    def __init__(self, rows: FakeRepo) -> None:
        self.rows = rows

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @asynccontextmanager
    async def begin(self):
        yield self


class FakeRepo:
    """The ``db.repositories`` functions the API uses, over dicts."""

    def __init__(self) -> None:
        self.users: dict[str, SimpleNamespace] = {}
        self.projects: dict[uuid.UUID, SimpleNamespace] = {}
        self.members: set[tuple[uuid.UUID, uuid.UUID]] = set()
        self.roles: dict[tuple[uuid.UUID, uuid.UUID], str] = {}
        self.documents: dict[uuid.UUID, SimpleNamespace] = {}

    def __call__(self) -> FakeSession:  # the session factory
        return FakeSession(self)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in [
            "upsert_user",
            "create_project",
            "list_projects",
            "get_member_project",
            "insert_document",
            "find_by_hash",
            "list_documents",
            "get_document",
            "get_document_state",
            "mark_processing",
            "mark_failed",
            "mark_summarizing",
            "mark_summarization_failed",
        ]:
            monkeypatch.setattr(repo, name, getattr(self, name))

    async def upsert_user(self, _session, principal):
        user = self.users.get(principal.subject)
        name = principal.name or principal.email
        if user is None:
            user = SimpleNamespace(
                id=uuid.uuid4(),
                actor_id=repo.actor_id_for(principal.email),
                auth_subject=principal.subject,
                email=principal.email,
                display_name=name,
            )
            self.users[principal.subject] = user
        user.email, user.display_name = principal.email, name
        return user

    async def create_project(self, _session, user_id, name):
        project = SimpleNamespace(id=uuid.uuid4(), name=name, created_by=user_id, created_at=_stamp())
        self.projects[project.id] = project
        self.members.add((project.id, user_id))
        self.roles[(project.id, user_id)] = "owner"
        return project

    async def get_member_project(self, _session, user_id, project_id):
        return self.projects.get(project_id) if (project_id, user_id) in self.members else None

    async def list_projects(self, _session, user_id, limit, cursor=None):
        rows = [p for p in self.projects.values() if (p.id, user_id) in self.members]
        return self._page(rows, limit, cursor)

    def _page(self, rows, limit, cursor):
        rows = sorted(rows, key=lambda r: (r.created_at, r.id), reverse=True)
        if cursor is not None:
            at, row_id = repo.decode_cursor(cursor)
            rows = [r for r in rows if (r.created_at, r.id) < (at, row_id)]
        items = rows[:limit]
        more = len(rows) > limit
        return repo.Page(
            items,
            repo.encode_cursor(items[-1].created_at, items[-1].id) if more else None,
        )

    async def find_by_hash(self, _session, project_id, content_hash):
        for doc in self.documents.values():
            if doc.project_id == project_id and doc.content_hash == content_hash:
                return doc
        return None

    async def insert_document(self, session, *, file, **fields):
        document_id, project_id, content_hash = file.document_id, fields["project_id"], file.content_hash
        existing = await self.find_by_hash(session, project_id, content_hash)
        if existing is not None:
            raise repo.DuplicateDocumentError(existing)
        doc = SimpleNamespace(
            id=document_id,
            project_id=project_id,
            content_hash=content_hash,
            original_filename=file.filename,
            size_bytes=file.size_bytes,
            original_key=file.original_key,
            uploaded_by=fields["uploaded_by"],
            status="uploaded",
            media_type="application/pdf",
            source_id=fields.get("source_id"),
            updated_at=datetime.now(UTC),
            error=None,
            markdown=None,
            summarization=None,
            summarization_status="none",
            summarization_model=None,
            summarization_error=None,
            summarized_at=None,
            created_at=_stamp(),
        )
        self.documents[document_id] = doc
        return doc

    async def list_documents(self, _session, project_id, limit, cursor=None):
        return self._page(
            [d for d in self.documents.values() if d.project_id == project_id],
            limit,
            cursor,
        )

    async def get_document(self, _session, project_id, document_id):
        doc = self.documents.get(document_id)
        return doc if doc is not None and doc.project_id == project_id else None

    get_document_state = get_document

    def _set(self, project_id, document_id, when, **values) -> bool:
        doc = self.documents.get(document_id)
        if doc is None or doc.project_id != project_id or not when(doc):
            return False
        for key, value in values.items():
            setattr(doc, key, value)
        return True

    async def mark_processing(self, _session, project_id, document_id, *, include_stale=False):
        def allowed(doc) -> bool:
            stale = (
                include_stale
                and doc.status in ("processing", "uploaded")
                and datetime.now(UTC) - doc.updated_at > timedelta(minutes=60)
            )
            return doc.status in ("uploaded", "failed") or stale

        return self._set(
            project_id,
            document_id,
            allowed,
            status="processing",
            error=None,
            updated_at=datetime.now(UTC),
        )

    async def mark_failed(self, _session, project_id, document_id, code):
        assert code in repo.PARSE_ERROR_CODES
        return self._set(project_id, document_id, lambda _: True, status="failed", error=code)

    async def mark_summarizing(self, _session, project_id, document_id):
        return self._set(
            project_id,
            document_id,
            lambda d: d.status == "parsed" and d.summarization_status != "running",
            summarization_status="running",
            summarization_error=None,
        )

    async def mark_summarization_failed(self, _session, project_id, document_id, code):
        assert code in repo.SUMMARIZATION_ERROR_CODES
        return self._set(
            project_id,
            document_id,
            lambda _: True,
            summarization_status="failed",
            summarization_error=code,
        )
