"""The Confluence importer (docs/sources/importers.md): a page and its descendants, or a space's pages, as documents.

Runs in the API process as the member the project's Atlassian is connected by. Each page is stored as it was read (JSON)
and as Markdown (``jira.storage_markdown``). With "follow links" the same-site pages the imported pages link to are read
too, one hop (their own links are not followed), and so are the Jira issues they link to (``jira.issue_document``). The
whole import holds at most ``MAX_PAGES`` pages and ``MAX_LINKED_ISSUES`` issues; a cap that is hit is logged (a count
only). A page or issue the member cannot see is skipped and counted as an item in error, the import goes on. Titles and
text live in documents only: events carry ids, kinds and counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from api.imports.atlassian import (
    CONVERT_FAILED,
    CONVERT_STEP,
    NO_ACCESS,
    READ_STEP,
    SKIPPABLE,
    Connections,
    access_of,
    confluence_client,
    import_failure,
    jira_reader,
)
from api.imports.issues import import_issue
from api.imports.model import ImportFailedError, ImportJob
from api.imports.writer import ImportWriter
from jira.confluence import ConfluenceClient
from jira.confluence_links import PageTarget, References, SpaceTarget, TitledPage, references
from jira.gateway import JiraError
from jira.issues import JiraIssueReader
from jira.markdown_safe import MarkupTooDeepError
from jira.storage_markdown import to_markdown
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.imports.confluence")

MAX_PAGES: Final = 100  # pages of one import: the target, its descendants and the followed pages
MAX_LINKED_ISSUES: Final = 50  # Jira issues of one import, followed from pages


@dataclass(frozen=True)
class ConfluenceImport:
    """What to import: a page tree or a space, and whether to follow the links of its pages."""

    target: PageTarget | SpaceTarget
    follow: bool


def page_markdown(page: dict[str, Any]) -> str:
    """The document of a page: its title as the heading, then the body."""

    storage = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    body = to_markdown(str(storage))
    return f"# {page.get('title', '')}\n\n{body}".rstrip() + "\n"


def page_storage(page: dict[str, Any]) -> str:
    return str(((page.get("body") or {}).get("storage") or {}).get("value") or "")


@dataclass(frozen=True)
class Inspection:
    """What a link would import, for the check: a page tree or a space, its title, how many pages (at most
    ``MAX_PAGES``; ``capped`` when there are more) and the Jira issues the first page links to (with "follow")."""

    kind: str
    title: str
    pages: int
    capped: bool
    linked_issues: int


async def inspect(confluence: ConfluenceClient, source: ConfluenceImport, host: str) -> Inspection:
    """Read the target (a page with its descendants, or a space's page list) and report what an import would take.
    ``JiraError`` when a read is refused."""

    if isinstance(source.target, PageTarget):
        page = await confluence.page(source.target.page_id)
        below, capped = await confluence.descendants(source.target.page_id, MAX_PAGES - 1)
        issues = len(references(page_storage(page), host).issue_keys) if source.follow else 0
        return Inspection("page", str(page.get("title", "")), 1 + len(below), capped, issues)
    space = await confluence.space(source.target.key)
    pages, capped = await confluence.space_pages(str(space["id"]), MAX_PAGES)
    return Inspection("space", str(space.get("name", "")), len(pages), capped, 0)


class ConfluenceImporter:
    """An ``Importer`` (``api.imports.runner``) of one Confluence link."""

    def __init__(self, connections: Connections, source: ConfluenceImport) -> None:
        self._connections = connections
        self._source = source

    async def __call__(self, job: ImportJob, writer: ImportWriter) -> None:
        access = await access_of(self._connections, job.user_id)
        if access is None:
            raise ImportFailedError(NO_ACCESS)
        try:
            async with confluence_client(access) as confluence, jira_reader(access) as jira:
                run = _Run(job, writer, confluence, jira, access.site_host)
                await run.import_all(self._source)
        except JiraError as error:
            raise import_failure(error) from None


class _Run:
    """One import: the pages read so far, the budget left and the links found."""

    def __init__(
        self, job: ImportJob, writer: ImportWriter, confluence: ConfluenceClient, jira: JiraIssueReader, host: str
    ) -> None:
        self._job = job
        self._writer = writer
        self._confluence = confluence
        self._jira = jira
        self._host = host
        self._seen: set[str] = set()
        self._found = References()
        self._titled: list[tuple[TitledPage, str | None]] = []  # a link by title and the space of the page it is on
        self._space_ids: dict[str, str | None] = {}

    async def import_all(self, source: ConfluenceImport) -> None:
        if isinstance(source.target, PageTarget):
            queue = await self._tree(source.target.page_id)
        else:
            space = await self._confluence.space(source.target.key)
            pages, capped = await self._confluence.space_pages(str(space["id"]), MAX_PAGES)
            if capped:
                self._cap_hit()
            queue = [str(page["id"]) for page in pages]
            for page in pages:
                await self._writer.declare(f"page:{page['id']}", str(page.get("title", "")))
        for page_id in queue:
            await self._page(page_id, follow=source.follow)
        if source.follow:
            await self._followed()

    async def _tree(self, root: str) -> list[str]:
        """The root (read now: its title is the first item) and the ids of its descendants, all declared."""

        below, capped = await self._confluence.descendants(root, MAX_PAGES - 1)
        if capped:
            self._cap_hit()
        for page in below:
            await self._writer.declare(f"page:{page['id']}", str(page.get("title", "")))
        return [root, *(str(page["id"]) for page in below)]

    async def _page(self, page_id: str, *, follow: bool) -> None:
        if page_id in self._seen:
            return
        self._seen.add(page_id)
        try:
            page = await self._confluence.page(page_id)
        except JiraError as error:
            if error.reason not in SKIPPABLE:
                raise
            await self._writer.fail(f"page:{page_id}", f"page {page_id}", reason=NO_ACCESS, step=READ_STEP)
            return
        title = str(page.get("title", ""))
        try:
            markdown = page_markdown(page)
        except MarkupTooDeepError:  # one page nested too deep to convert: its item is an error, the import goes on
            await self._writer.fail(f"page:{page_id}", title, reason=CONVERT_FAILED, step=CONVERT_STEP)
            return
        await self._writer.add(
            f"page:{page_id}", name=title, original=json.dumps(page, sort_keys=True).encode(), markdown=markdown
        )
        if follow:
            try:
                found = references(page_storage(page), self._host)
            except RecursionError:  # as deep as a page the converter refused: no links are followed from it
                return
            self._found.page_ids.extend(found.page_ids)
            self._found.issue_keys.extend(found.issue_keys)
            space_id = str(page["spaceId"]) if page.get("spaceId") else None
            self._titled.extend((titled, space_id) for titled in found.titled)

    async def _followed(self) -> None:
        """The pages the imported pages link to, one hop, then the Jira issues they link to."""

        for page_id in dict.fromkeys(self._found.page_ids):
            if page_id not in self._seen:
                if not self._room():
                    break
                await self._page(page_id, follow=False)
        for titled, default_space in self._titled:
            if not self._room():
                break
            resolved = await self._resolve(titled, default_space)
            if resolved is not None:
                await self._page(resolved, follow=False)
        await self._issues(list(dict.fromkeys(self._found.issue_keys)))

    def _room(self) -> bool:
        if len(self._seen) < MAX_PAGES:
            return True
        self._cap_hit()
        return False

    async def _resolve(self, titled: TitledPage, default_space: str | None) -> str | None:
        """The id of a page linked by title (in the link's space, else the space of the page it is on); ``None`` when
        it is not found or the member cannot see it."""

        space_id = default_space
        if titled.space_key is not None:
            if titled.space_key not in self._space_ids:
                self._space_ids[titled.space_key] = await self._space_id(titled.space_key)
            space_id = self._space_ids[titled.space_key]
        if space_id is None:
            return None
        try:
            return await self._confluence.find_page(space_id, titled.title)
        except JiraError as error:
            if error.reason not in SKIPPABLE:
                raise
            return None

    async def _space_id(self, key: str) -> str | None:
        try:
            return str((await self._confluence.space(key))["id"])
        except JiraError as error:
            if error.reason not in SKIPPABLE:
                raise
            return None

    async def _issues(self, keys: list[str]) -> None:
        if len(keys) > MAX_LINKED_ISSUES:
            log_event(
                logger,
                WARNING,
                "api.imports.issue_cap_reached",
                project_id=str(self._job.project_id),
                cap=MAX_LINKED_ISSUES,
            )
        for key in keys[:MAX_LINKED_ISSUES]:
            await import_issue(self._writer, self._jira, key)

    def _cap_hit(self) -> None:
        log_event(logger, WARNING, "api.imports.page_cap_reached", project_id=str(self._job.project_id), cap=MAX_PAGES)
