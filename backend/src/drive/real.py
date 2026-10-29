"""Real Drive gateway: ADC, ``drive.readonly``, google-api-python-client.

Credentials are shared; the service object (and its httplib2 client, which is
not thread-safe) is built per call. Failures become ``DriveError`` with a
closed reason; Google's text is never kept, logged or chained.
"""

from __future__ import annotations

import contextlib
import io
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Final, Literal

import google.auth
import httplib2
from google.auth.exceptions import GoogleAuthError, TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from drive.categories import FOLDER_MIME, GOOGLE
from drive.gateway import (
    CHUNK_SIZE,
    LIST_PAGE_SIZE,
    LIST_PARENT_BATCH,
    NUM_RETRIES,
    SCOPES,
    DriveError,
)
from drive.links import is_drive_id
from drive.model import DriveFolder, DriveItem, Listing
from drive.reasons import Reason, google_reason_from_content, reason_for_http
from logger.logger import ERROR, get_logger, log_event

logger = get_logger("drive.real")

ServiceFactory = Callable[[str, str], Any]

_DOCUMENT: Final = GOOGLE + "document"
_PRESENTATION: Final = GOOGLE + "presentation"
_SPREADSHEET: Final = GOOGLE + "spreadsheet"
_LIST_FIELDS: Final = "nextPageToken, files(id,name,mimeType,size,modifiedTime,version,md5Checksum,parents)"
_IMAGE_LINK_KEYS: Final = frozenset({"contentUri", "sourceUri"})


def _error_from_http(error: HttpError) -> DriveError:
    try:
        status = int(error.resp.status)
    except (AttributeError, TypeError, ValueError):
        status = 0
    token = google_reason_from_content(getattr(error, "content", None))
    return DriveError(reason_for_http(status, token), http_status=status or None, google_reason=token)


@contextlib.contextmanager
def mapped_errors() -> Iterator[None]:
    """Map every failure inside the block to DriveError; no Google text is kept."""

    try:
        yield
    except HttpError as error:
        raise _error_from_http(error) from None
    except TransportError:
        raise DriveError(Reason.DRIVE_UNAVAILABLE) from None
    except GoogleAuthError:
        raise DriveError(Reason.SETUP_ERROR) from None
    except (httplib2.HttpLib2Error, OSError, TimeoutError):
        raise DriveError(Reason.DRIVE_UNAVAILABLE) from None
    except Exception as error:  # the boundary maps every failure; an already mapped one passes unchanged
        if isinstance(error, DriveError):
            raise
        raise DriveError(Reason.UNKNOWN_ERROR) from None


def execute(request: Any) -> Any:
    """Run one Google request with retries and map every failure to DriveError."""

    with mapped_errors():
        return request.execute(num_retries=NUM_RETRIES)


def _quote_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _strip_image_links(value: Any) -> Any:
    """Drop Google image links (``contentUri``) from raw Docs/Slides JSON."""

    if isinstance(value, dict):
        return {k: _strip_image_links(v) for k, v in value.items() if k not in _IMAGE_LINK_KEYS}
    if isinstance(value, list):
        return [_strip_image_links(v) for v in value]
    return value


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class GoogleDriveGateway:
    mode: Literal["real"] = "real"

    def __init__(
        self,
        *,
        credentials: Any | None = None,
        service_factory: ServiceFactory | None = None,
        identity_email: str | None = None,
    ) -> None:
        self._identity_email = (identity_email or "").strip()
        self._credentials_value = credentials
        self._factory = service_factory
        self._lock = threading.Lock()

    def _credentials(self) -> Any:
        with self._lock:
            if self._credentials_value is None:
                try:
                    self._credentials_value, _ = google.auth.default(scopes=list(SCOPES))
                except GoogleAuthError:
                    raise DriveError(Reason.SETUP_ERROR) from None
            return self._credentials_value

    def _service(self, api: str, version: str) -> Any:
        if self._factory is not None:
            return self._factory(api, version)
        return build(api, version, credentials=self._credentials(), cache_discovery=False)

    def identity(self) -> str:
        # No fallback: a blank value is a setup error, never an ADC guess (SPL-DRV-01).
        if not self._identity_email:
            log_event(logger, ERROR, "drive.identity.missing")
            raise DriveError(Reason.SETUP_ERROR)
        self._credentials()
        return self._identity_email

    def check_folder(self, folder_id: str) -> DriveFolder:
        if not is_drive_id(folder_id):
            raise DriveError(Reason.INVALID_LINK)
        meta = execute(
            self._service("drive", "v3")
            .files()
            .get(
                fileId=folder_id,
                fields="id,name,mimeType,driveId",
                supportsAllDrives=True,
            )
        )
        if not isinstance(meta, dict) or not isinstance(meta.get("mimeType"), str):
            raise DriveError(Reason.UNKNOWN_ERROR)
        if meta["mimeType"] != FOLDER_MIME:
            raise DriveError(Reason.NOT_A_FOLDER)
        if not isinstance(meta.get("id"), str) or not isinstance(meta.get("name"), str):
            raise DriveError(Reason.UNKNOWN_ERROR)
        return DriveFolder(id=meta["id"], name=meta["name"], in_shared_drive=bool(meta.get("driveId")))

    def list_all(self, folder_id: str, *, deadline: float | None = None) -> Listing:
        root = self.check_folder(folder_id)
        service = self._service("drive", "v3")
        visited = {root.id}
        level = [root.id]
        items: dict[str, DriveItem] = {}
        revisited = 0
        while level:
            next_level: list[str] = []
            for start in range(0, len(level), LIST_PARENT_BATCH):
                batch = level[start : start + LIST_PARENT_BATCH]
                for child in self._children(service, batch, deadline):
                    if (
                        not isinstance(child, dict)
                        or not isinstance(child.get("id"), str)
                        or not isinstance(child.get("mimeType"), str)
                    ):
                        raise DriveError(Reason.UNKNOWN_ERROR)
                    if child["mimeType"] == FOLDER_MIME:
                        if child["id"] in visited:
                            revisited += 1
                        elif is_drive_id(child["id"]):
                            visited.add(child["id"])
                            next_level.append(child["id"])
                    elif child["id"] not in items:
                        parents = child.get("parents") or []
                        parent = next((p for p in parents if p in batch), batch[0])
                        items[child["id"]] = DriveItem(
                            id=child["id"],
                            name=child.get("name", ""),
                            mime_type=child["mimeType"],
                            parent_id=parent,
                            size=_int_or_none(child.get("size")),
                            modified_time=child.get("modifiedTime"),
                            version=_int_or_none(child.get("version")),
                            md5=child.get("md5Checksum"),
                        )
            level = next_level
        return Listing(
            root=root,
            folder_count=len(visited),
            items=tuple(items.values()),
            revisited=revisited,
        )

    def _children(self, service: Any, parents: list[str], deadline: float | None) -> Iterator[Any]:
        # Ids are checked before they enter a ``q`` string (they are Drive's own ids).
        if not all(is_drive_id(p) for p in parents):
            raise DriveError(Reason.INVALID_LINK)
        q = "(" + " or ".join(f"'{p}' in parents" for p in parents) + ") and trashed = false"
        token: str | None = None
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise DriveError(Reason.TOO_SLOW)
            page = execute(
                service.files().list(
                    q=q,
                    fields=_LIST_FIELDS,
                    pageSize=LIST_PAGE_SIZE,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
            )
            if not isinstance(page, dict) or not isinstance(page.get("files", []), list):
                raise DriveError(Reason.UNKNOWN_ERROR)
            yield from page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                return

    def iter_download(self, file_id: str, *, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        if not is_drive_id(file_id):
            raise DriveError(Reason.INVALID_LINK)
        request = self._service("drive", "v3").files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request, chunksize=chunk_size)
        done = False
        while not done:
            with mapped_errors():
                _, done = downloader.next_chunk(num_retries=NUM_RETRIES)
            data = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
            if data:
                yield data

    def read_google(self, file_id: str, mime_type: str) -> dict[str, Any]:
        if not is_drive_id(file_id):
            raise DriveError(Reason.INVALID_LINK)
        if mime_type == _DOCUMENT:
            raw = execute(self._service("docs", "v1").documents().get(documentId=file_id, includeTabsContent=True))
        elif mime_type == _PRESENTATION:
            raw = execute(self._service("slides", "v1").presentations().get(presentationId=file_id))
        elif mime_type == _SPREADSHEET:
            raw = self._read_sheet(file_id)
        else:
            raise DriveError(Reason.UNKNOWN_ERROR)
        stripped: dict[str, Any] = _strip_image_links(raw)
        return stripped

    def _read_sheet(self, file_id: str) -> dict[str, Any]:
        sheets = self._service("sheets", "v4").spreadsheets()
        meta = execute(
            sheets.get(
                spreadsheetId=file_id,
                fields=("properties.title,sheets.properties(sheetId,title,index,sheetType)"),
            )
        )
        try:
            props = [s["properties"] for s in meta.get("sheets", [])]
            # Only GRID tabs have cells; other tabs (charts) keep an empty slot so
            # valueRanges[i] stays aligned with sheets[i].
            grid = [i for i, p in enumerate(props) if p.get("sheetType", "GRID") == "GRID"]
            titles = [props[i]["title"] for i in grid]
        except (AttributeError, KeyError, TypeError):
            raise DriveError(Reason.UNKNOWN_ERROR) from None
        fetched: list[dict[str, Any]] = []
        for start in range(0, len(titles), LIST_PARENT_BATCH):
            chunk = titles[start : start + LIST_PARENT_BATCH]
            fetched.extend(
                execute(
                    sheets.values().batchGet(
                        spreadsheetId=file_id,
                        ranges=[_quote_sheet(t) for t in chunk],
                        valueRenderOption="FORMATTED_VALUE",
                    )
                ).get("valueRanges", [])
            )
        ranges: list[dict[str, Any]] = [{} for _ in props]
        for slot, value in zip(grid, fetched, strict=False):
            ranges[slot] = value
        return {"spreadsheet": meta, "valueRanges": ranges}
