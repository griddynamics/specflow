"""The diagrams.net link source (docs/sources/importers.md): check a link, then import the diagram.

Any member may do both; there is no integration gate and no key. The link is parsed, fetched (``#U``), decoded and
counted inside the request, so every refusal is an ``ApiError`` here. The import receives the decoded pages. Links,
labels and hosts are never logged; events carry ids and counts.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.errors import ApiError
from api.imports.drawio import DrawioImport, DrawioImporter, named
from api.imports.drawio_link import DrawioLink, DrawioLinkError, parse_link
from api.schemas import BatchSourceOut, DrawioCheckOut, DrawioIn, DrawioPageOut
from api.source_imports import StartedImport, start_import
from file_parsers import diagrams
from file_parsers.diagrams import MAX_INPUT_BYTES, Diagram, DiagramDecodeError
from integrations.safe_fetch import SafeFetchError, fetch_public
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.source_drawio")
router = APIRouter(prefix="/projects/{pid}/sources/drawio", tags=["sources"])
KIND = "drawio"
FETCH_TIMEOUT_S = 15.0
PNG_MAGIC = b"\x89PNG"


def drawio_link_invalid() -> ApiError:
    return ApiError(422, "drawio_link_invalid", "Paste a link to a diagram on diagrams.net.")


def drawio_link_unsupported(storage: str | None = None) -> ApiError:
    where = f"in {storage}" if storage else "in a place SpecFlow cannot read from a link"
    return ApiError(
        422,
        "drawio_link_unsupported",
        f"The diagram is stored {where}. Download it as .drawio and add it with Upload.",
    )


def drawio_fetch_failed() -> ApiError:
    return ApiError(409, "drawio_fetch_failed", "The diagram could not be fetched from the link.")


def drawio_not_a_diagram() -> ApiError:
    return ApiError(422, "drawio_not_a_diagram", "The link does not hold a diagrams.net diagram.")


def drawio_too_large() -> ApiError:
    return ApiError(413, "drawio_too_large", "The diagram is too large to import.")


def _refused(error: DrawioLinkError) -> ApiError:
    if error.code == "too_large":
        return drawio_too_large()
    if error.code == "unsupported":
        return drawio_link_unsupported(error.storage)
    return drawio_link_invalid()


def _hint(data: bytes) -> str:
    """The file kind the fetched bytes are, by their content (a PNG, an SVG), else plain XML."""

    if data.startswith(PNG_MAGIC):
        return "diagram.drawio.png"
    head = data[:2048].removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "diagram.drawio.svg"
    return "diagram.drawio"


async def _diagram(link: DrawioLink) -> Diagram:
    try:
        if link.kind == "payload":
            found = await asyncio.to_thread(diagrams.decode_link_payload, link.value)
        elif link.kind == "xml":
            found = await asyncio.to_thread(lambda: diagrams.decode(link.value.encode(), hint="diagram.drawio"))
        else:
            data = await fetch_public(link.value, max_bytes=MAX_INPUT_BYTES, timeout_s=FETCH_TIMEOUT_S)
            found = await asyncio.to_thread(lambda: diagrams.decode(data, hint=_hint(data)))
    except SafeFetchError as error:
        if error.code == "too_large":
            raise drawio_too_large() from None
        raise (drawio_link_invalid() if error.code == "invalid_url" else drawio_fetch_failed()) from None
    except DiagramDecodeError as error:
        raise (drawio_too_large() if error.code == "too_large" else drawio_not_a_diagram()) from None
    if not found.pages:
        raise drawio_not_a_diagram()
    return named(found, link.name)


async def _loaded(link: str) -> tuple[DrawioLink, Diagram]:
    try:
        parsed = parse_link(link)
    except DrawioLinkError as error:
        raise _refused(error) from None
    return parsed, await _diagram(parsed)


@router.post("/check", response_model=DrawioCheckOut)
async def check_drawio(body: DrawioIn, access: MemberDep) -> DrawioCheckOut:
    _, found = await _loaded(body.link)
    log_event(logger, INFO, "api.imports.checked", project_id=str(access.project.id), kind=KIND, pages=len(found.pages))
    return DrawioCheckOut(
        name=found.name,
        pages=[DrawioPageOut(name=p.name, shapes=len(p.shapes), arrows=len(p.arrows)) for p in found.pages],
        capped=found.capped,
    )


@router.post("", status_code=201, response_model=BatchSourceOut)
async def add_drawio_source(body: DrawioIn, runtime: RuntimeDep, user: UserDep, access: MemberDep) -> BatchSourceOut:
    pid = access.project.id
    parsed, found = await _loaded(body.link)
    created = await start_import(
        runtime,
        pid,
        user.id,
        started=StartedImport(KIND, DrawioImporter(DrawioImport(found)), user.id),
        title=found.name,
        link=parsed.stored,
    )
    log_event(
        logger,
        INFO,
        "api.imports.added",
        user_id=str(user.id),
        project_id=str(pid),
        kind=KIND,
        form=parsed.kind,
        pages=len(found.pages),
        capped=found.capped,
    )
    return created
