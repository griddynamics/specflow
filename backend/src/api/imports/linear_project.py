"""The Linear importer (docs/sources/importers.md): a Linear project (its issues and its documents) or one issue (itself
and its sub-issues), each issue with its comments, as documents.

Runs in the API process as the member the project's Linear is connected by. The issues are read through the gateway
(a project's in pages, an issue's sub-issues level by level); an issue the member cannot see is never returned by
Linear, and one whose comments vanish or are refused while they are read is skipped and counted as an item in error.
At most ``MAX_LINEAR_ISSUES`` issues are taken (the cap is logged as a count). The import is a snapshot (D7): adding
the same link again is a new source, and the writer's content hash drops what the project already holds. Text lives in
documents only: events carry ids, the kind and counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from api.imports.linear_access import NO_ACCESS, READ_STEP, LinearGateways, import_failure
from api.imports.model import ImportFailedError, ImportJob
from api.imports.writer import ImportWriter
from linear.errors import LinearError, LinearForbiddenError, LinearNotFoundError
from linear.gateway import LinearGateway
from linear.issue_document import (
    clean_text,
    document_markdown,
    document_original,
    document_title,
    issue_markdown,
    issue_original,
    issue_title,
)
from linear.models import Document, Issue, Project
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.imports.linear_project")

MAX_LINEAR_ISSUES: Final = 200  # issues taken by one import (the issue of an issue source is one more)
SKIPPABLE: Final = (LinearNotFoundError, LinearForbiddenError)  # an item the member cannot see: skipped, counted
Kind = Literal["project", "issue"]


@dataclass(frozen=True)
class LinearImport:
    """What to import: the project or issue with this Linear id and every issue under it whose identifier is not in
    ``exclude`` (the issue of an issue source is always imported)."""

    kind: Kind
    target_id: str
    exclude: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LinearItem:
    """One issue the check lists: its identifier, title, state name and the identifier of its parent when known."""

    identifier: str
    title: str
    state: str
    parent: str | None


@dataclass(frozen=True)
class LinearTarget:
    """A project or issue a link names: its kind, Linear id, name (shown, stored as the source's name) and URL."""

    kind: Kind
    target_id: str
    name: str
    url: str


@dataclass(frozen=True)
class LinearInspection:
    """What a link would import, for the check: the target and how many issues it has (at most the cap)."""

    target: LinearTarget
    issues: int
    items: tuple[LinearItem, ...]
    capped: bool
    documents: int


@dataclass(frozen=True)
class LinearRead:
    """What an import reads: the issue of an issue source, the issues under the target, the project documents (the
    project's own overview first)."""

    root: Issue | None
    issues: tuple[Issue, ...]
    documents: tuple[Document, ...]
    capped: bool


async def project_issues(gateway: LinearGateway, project_id: str, cap: int) -> tuple[list[Issue], bool]:
    """The issues of a project in pages, at most ``cap``; whether there are more."""

    found: list[Issue] = []
    cursor: str | None = None
    while True:
        page = await gateway.issues_of_project(project_id, cursor)
        found.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return found[:cap], len(found) > cap
        if len(found) >= cap:
            return found[:cap], True


async def sub_issues(gateway: LinearGateway, issue_id: str, cap: int) -> tuple[list[Issue], bool]:
    """The sub-issues of an issue, children before grandchildren, at most ``cap``; whether there are more."""

    found: list[Issue] = []
    seen = {issue_id}
    level = [issue_id]
    while level:
        below: list[Issue] = []
        for parent in level:
            below += [child for child in await gateway.children(parent) if child.id not in seen]
        seen.update(child.id for child in below)
        found += below
        if len(found) > cap:
            return found[:cap], True
        level = [child.id for child in below]
    return found, False


def project_overview(project: Project) -> Document | None:
    """The project's own text (its short description and its Markdown overview) as one document, under the project's
    id; none when both are empty."""

    text = "\n\n".join(part.strip() for part in (project.description, project.content) if part and part.strip())
    return Document(project.id, project.id, project.name, text, project.url) if text else None


async def _read(gateway: LinearGateway, source: LinearImport) -> LinearRead:
    if source.kind == "project":
        project = await gateway.project(source.target_id)
        issues, capped = await project_issues(gateway, source.target_id, MAX_LINEAR_ISSUES)
        overview = project_overview(project)
        documents = (*([overview] if overview else []), *await gateway.project_documents(source.target_id))
        return LinearRead(None, tuple(issues), documents, capped)
    root = await gateway.issue(source.target_id)
    issues, capped = await sub_issues(gateway, root.id, MAX_LINEAR_ISSUES)
    return LinearRead(root, tuple(issues), (), capped)


def _items(issues: tuple[Issue, ...], known: dict[str, str]) -> tuple[LinearItem, ...]:
    return tuple(
        LinearItem(i.identifier, clean_text(i.title), i.state.name, known.get(i.parent_id or "")) for i in issues
    )


async def resolve_target(gateway: LinearGateway, link: str) -> LinearTarget:
    """The project or issue a pasted link or identifier names (``LinearNotFoundError`` when the member cannot see it,
    ``LinearInvalidError`` when it is neither); reads nothing under it."""

    found = await gateway.resolve_link(link)
    if isinstance(found, Project):
        return LinearTarget("project", found.id, clean_text(found.name), found.url)
    return LinearTarget("issue", found.id, issue_title(found), found.url)


async def inspect_link(gateway: LinearGateway, link: str) -> LinearInspection:
    """List what an import of ``link`` would take (``LinearError`` when a read is refused); stores nothing."""

    target = await resolve_target(gateway, link)
    read = await _read(gateway, LinearImport(target.kind, target.target_id))
    known = {issue.id: issue.identifier for issue in (*read.issues, *([read.root] if read.root else []))}
    return LinearInspection(target, len(read.issues), _items(read.issues, known), read.capped, len(read.documents))


class LinearProjectImporter:
    """An ``Importer`` (``api.imports.runner``) of one Linear project or issue."""

    def __init__(self, connections: LinearGateways, source: LinearImport) -> None:
        self._connections = connections
        self._source = source

    async def __call__(self, job: ImportJob, writer: ImportWriter) -> None:
        view = await self._connections.get(job.user_id)
        if view is None or view.status != "active":
            raise ImportFailedError(NO_ACCESS)
        try:
            async with self._connections.gateway(job.user_id) as gateway:
                read = await _read(gateway, self._source)
                if read.capped:
                    log_event(
                        logger,
                        WARNING,
                        "api.imports.linear_cap_reached",
                        project_id=str(job.project_id),
                        cap=MAX_LINEAR_ISSUES,
                    )
                await self._store(gateway, writer, read)
        except LinearError as error:
            raise import_failure(error) from None

    async def _store(self, gateway: LinearGateway, writer: ImportWriter, read: LinearRead) -> None:
        known = {issue.id: issue for issue in (*read.issues, *([read.root] if read.root else []))}
        taken = [issue for issue in read.issues if issue.identifier not in self._source.exclude]
        issues = [*([read.root] if read.root else []), *taken]
        for issue in issues:
            await writer.declare(f"issue:{issue.identifier}", issue_title(issue))
        for document in read.documents:
            await writer.declare(f"document:{document.id}", document_title(document))
        for issue in issues:
            await _import_issue(writer, gateway, issue, known)
        for document in read.documents:
            await writer.add(
                f"document:{document.id}",
                name=document_title(document),
                original=document_original(document),
                markdown=document_markdown(document),
            )


async def _parent_of(gateway: LinearGateway, issue: Issue, known: dict[str, Issue]) -> Issue | None:
    """The parent as the document names it; one outside the import is read once, one the member cannot see is none."""

    if issue.parent_id is None:
        return None
    if issue.parent_id not in known:
        try:
            known[issue.parent_id] = await gateway.issue(issue.parent_id)
        except SKIPPABLE:
            return None
    return known[issue.parent_id]


async def _import_issue(writer: ImportWriter, gateway: LinearGateway, issue: Issue, known: dict[str, Issue]) -> None:
    """Store ``issue`` with its comments as ``issue:<IDENTIFIER>``; one whose comments Linear says are missing or
    forbidden is one item in error (``no_access``, step ``read``). Any other refusal raises for the importer."""

    external_id = f"issue:{issue.identifier}"
    try:
        comments = await gateway.comments(issue.id)
        parent = await _parent_of(gateway, issue, known)
    except SKIPPABLE:
        await writer.fail(external_id, issue.identifier, reason=NO_ACCESS, step=READ_STEP)
        return
    await writer.add(
        external_id,
        name=issue_title(issue),
        original=issue_original(issue, parent, comments),
        markdown=issue_markdown(issue, parent, comments),
    )
