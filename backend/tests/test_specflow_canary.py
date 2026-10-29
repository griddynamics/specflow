"""AC-16.4 canary: a run as project A can never read or change project B's data.

Seeds projects A and B (scratch Postgres at head, session test agent role with
RLS). Every tool activity is run as A, including with B's document id, and
every result is scanned for B's ids and B's text. B's rows are compared before
and after. Layers: the tool layer (ids from the run context), Postgres RLS +
column grants.

Gate: ``SPECFLOW_TEST_POSTGRES=1`` (no model, no Temporal server: activities
run in ``ActivityEnvironment``).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from db.agent_scope import (
    agent_scope,
    build_agent_session_factory,
    dispose_agent_session_factory,
)
from db.models import Document, Project, ProjectMember, User
from db.session import build_engine, build_session_factory
from specflow_live import requires_live
from temporal.agents.activities import SummarizeActivities
from temporal.agents.models import (
    MarkFailedInput,
    ReadChunkInput,
    SaveSummarizationInput,
)

pytestmark = [requires_live("POSTGRES")]

B_TEXT = "BRAVO-CANARY-TEXT-5d7e"


@dataclass
class Canary:
    acts: SummarizeActivities
    app_sessions: object
    agent_sessions: object
    project_a: uuid.UUID
    project_b: uuid.UUID
    doc_a: uuid.UUID
    doc_b: uuid.UUID

    @property
    def b_markers(self) -> tuple[str, ...]:
        return (str(self.project_b), str(self.doc_b), B_TEXT, "bravo.pdf")


def _document(project_id: uuid.UUID, label: str, markdown: str, filename: str) -> Document:
    return Document(
        project_id=project_id,
        content_hash=hashlib.sha256(label.encode()).hexdigest(),
        original_filename=filename,
        media_type="application/pdf",
        size_bytes=10,
        origin="upload",
        original_key=f"projects/{project_id}/documents/x/original.pdf",
        markdown=markdown,
        status="parsed",
        summarization=f"summary of {label}",
        summarization_status="succeeded",
    )


@pytest_asyncio.fixture
async def canary(migrated_database, agent_role) -> AsyncIterator[Canary]:
    engine = build_engine(migrated_database)
    app_sessions = build_session_factory(engine)
    agent_sessions = build_agent_session_factory(migrated_database, agent_role.settings())
    tag = uuid.uuid4().hex[:8]
    async with app_sessions() as session, session.begin():
        user = User(actor_id=f"human:canary-{tag}", display_name="C", email="c@example.com")
        session.add(user)
        await session.flush()
        a, b = (
            Project(name="A", created_by=user.id),
            Project(name="B", created_by=user.id),
        )
        session.add_all([a, b])
        await session.flush()
        session.add_all(ProjectMember(project_id=p.id, user_id=user.id) for p in (a, b))
        doc_a = _document(a.id, f"a-{tag}", "alpha text " * 50, "alpha.pdf")
        doc_b = _document(b.id, f"b-{tag}", f"{B_TEXT} " * 50, "bravo.pdf")
        session.add_all([doc_a, doc_b])
    fixture = Canary(
        acts=SummarizeActivities(app_sessions, agent_sessions),
        app_sessions=app_sessions,
        agent_sessions=agent_sessions,
        project_a=a.id,
        project_b=b.id,
        doc_a=doc_a.id,
        doc_b=doc_b.id,
    )
    try:
        yield fixture
    finally:
        try:
            await dispose_agent_session_factory(agent_sessions)
        finally:
            await engine.dispose()


async def _b_state(c: Canary) -> tuple:
    async with c.app_sessions() as session:
        row = await session.scalar(select(Document).where(Document.id == c.doc_b))
        return (
            row.markdown,
            row.summarization,
            row.summarization_status,
            row.summarization_error,
        )


def _assert_no_b(c: Canary, value: object) -> None:
    dumped = repr(value)
    for marker in c.b_markers:
        assert marker not in dumped, "project B data leaked into a project A result"


async def test_every_tool_activity_run_as_a_never_sees_or_changes_b(canary: Canary):
    c, env = canary, ActivityEnvironment()
    before = await _b_state(c)

    # A's own document works and contains no B data.
    chunk = await env.run(
        c.acts.read_document_chunk,
        ReadChunkInput(project_id=c.project_a, document_id=c.doc_a, offset=0),
    )
    assert "alpha text" in chunk.text
    _assert_no_b(c, chunk)

    # As A, B's document id is invisible to every tool.
    with pytest.raises(ApplicationError) as read_b:
        await env.run(
            c.acts.read_document_chunk,
            ReadChunkInput(project_id=c.project_a, document_id=c.doc_b, offset=0),
        )
    assert read_b.value.message == "document_not_found"

    # The trusted writes are bound to the input project: B's row is untouched.
    with pytest.raises(ApplicationError) as save_b:
        await env.run(
            c.acts.save_summarization,
            SaveSummarizationInput(
                project_id=c.project_a,
                document_id=c.doc_b,
                summary="overwrite attempt",
                title="X",
                model="m",
                workflow_id="wf",
                requested_by=uuid.uuid4(),
            ),
        )
    assert save_b.value.message == "document_not_found"
    await env.run(
        c.acts.mark_summarization_failed,
        MarkFailedInput(project_id=c.project_a, document_id=c.doc_b, error_code="tool_failed"),
    )

    assert await _b_state(c) == before


async def test_rls_hides_b_even_without_a_project_filter(canary: Canary):
    """The Postgres layer alone: scope A sees only A's rows, by any query."""

    c = canary
    async with agent_scope(c.agent_sessions, c.project_a) as session:
        rows = (await session.execute(text("SELECT id, markdown FROM documents"))).all()
        by_id = (await session.execute(text("SELECT markdown FROM documents WHERE id = :d"), {"d": c.doc_b})).all()
    assert {row.id for row in rows} == {c.doc_a}
    assert by_id == []
    _assert_no_b(c, rows)
