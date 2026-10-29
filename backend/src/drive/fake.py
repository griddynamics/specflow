"""Fake Drive for ``APP_ENV=local`` (NFR-PORT-0002): no network, recorded folders.

Reserved folder ids (paste as ``https://drive.google.com/drive/folders/<id>``):
see ``fixtures/contract.json``. Any other valid id answers ``no_access``, as a
real 404 does.

Roles: the API and the import worker each build their own gateway in separate
processes, so ``fake-import-fails`` cannot count calls. The fake knows its
``role`` instead: only the ``worker`` role fails the listing of that folder
(FR-GDUI-0018.AC4); in the ``api`` role Test connection and Save succeed.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Final, Literal

from drive import recordings
from drive.categories import GOOGLE
from drive.gateway import CHUNK_SIZE, FAKE_DRIVE_EMAIL, DriveError
from drive.links import is_drive_id
from drive.model import DriveFolder, DriveItem, Listing
from drive.reasons import Reason
from settings import ConfigurationError

EMPTY_FOLDER: Final = "fake-empty-folder"
FORMS_ONLY: Final = "fake-forms-only"
IMPORT_FAILS: Final = "fake-import-fails"
IMPORT_FAILS_LOOKS_LIKE: Final = "fake-synthetic-23"
ROLE_API: Final = "api"
ROLE_WORKER: Final = "worker"
REASON_PREFIX: Final = "fake-reason-"
_REASONS: Final = {
    r.value: r
    for r in (
        Reason.NO_ACCESS,
        Reason.NOT_A_FOLDER,
        Reason.DRIVE_UNAVAILABLE,
        Reason.SETUP_ERROR,
        Reason.TOO_SLOW,
        Reason.UNKNOWN_ERROR,
    )
}
_SAMPLE_OF: Final = {
    GOOGLE + "document": "document.json",
    GOOGLE + "presentation": "presentation.json",
    GOOGLE + "spreadsheet": "spreadsheet.json",
}


def _tiny_pdf() -> bytes:
    """A valid 1-page PDF, so the real parser stays fast."""

    text = b"BT /F1 12 Tf 20 100 Td (Fake Drive file) Tj ET"
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        xref,
    )
    return out


TINY_PDF: Final = _tiny_pdf()


def _wait(seconds: float) -> None:
    """Block for ``seconds`` without a sleep call (the fake plays a slow Drive)."""

    threading.Event().wait(seconds)


class FakeDriveGateway:
    mode: Literal["fake"] = "fake"

    def __init__(
        self,
        *,
        app_env: str,
        fixtures: Traversable | Path | None = None,
        google_samples: Traversable | Path | None = None,
        file_delay_s: float = 1.0,
        role: str = ROLE_API,
        sleep: Callable[[float], None] = _wait,
    ) -> None:
        if app_env != "local":
            raise ConfigurationError(("APP_ENV",))
        if role not in (ROLE_API, ROLE_WORKER):
            raise ValueError(f"unknown fake Drive role {role!r}")
        self._role = role
        self._fixtures = recordings.as_traversable(fixtures)
        self._samples = google_samples
        self._delay = file_delay_s
        self._sleep = sleep
        self._trees: dict[str, recordings.RecordedTree] = {}

    # -- gateway -----------------------------------------------------------
    def identity(self) -> str:
        return FAKE_DRIVE_EMAIL

    def check_folder(self, folder_id: str) -> DriveFolder:
        if not is_drive_id(folder_id):
            raise DriveError(Reason.INVALID_LINK)
        if folder_id.startswith(REASON_PREFIX):
            reason = _REASONS.get(folder_id.removeprefix(REASON_PREFIX))
            raise DriveError(reason or Reason.NO_ACCESS)
        special = self._special(folder_id)
        if special is not None:
            return special
        tree = self._tree_of(folder_id)
        if tree is not None and folder_id in tree.by_id:
            raise DriveError(Reason.NOT_A_FOLDER)
        if tree is None or folder_id not in tree.folders:
            raise DriveError(Reason.NO_ACCESS)
        return tree.folders[folder_id]

    def list_all(self, folder_id: str, *, deadline: float | None = None) -> Listing:
        if folder_id == REASON_PREFIX + Reason.TOO_SLOW.value:
            # The one slow answer: waits out the caller's deadline, then fails.
            if deadline is not None:
                self._sleep(max(0.0, deadline - time.monotonic()))
            raise DriveError(Reason.TOO_SLOW)
        if folder_id == IMPORT_FAILS and self._role == ROLE_WORKER:
            # Test and Save (api role) pass; only the import's listing fails.
            raise DriveError(Reason.NO_ACCESS)
        root = self.check_folder(folder_id)
        if folder_id == EMPTY_FOLDER:
            return Listing(root, 1, (), 0)
        if folder_id == FORMS_ONLY:
            return Listing(root, 1, tuple(self._forms(folder_id)), 0)
        if folder_id == IMPORT_FAILS:
            # api role: the folder looks like synthetic-23 (the demo mock does too).
            tree = self._tree_of(IMPORT_FAILS_LOOKS_LIKE)
            if tree is None:
                raise DriveError(Reason.NO_ACCESS)
            items, folders = self._walk(tree, IMPORT_FAILS_LOOKS_LIKE)
            return Listing(root, folders, items, 0)
        tree = self._tree_of(folder_id)
        if tree is None:  # check_folder found it, so this cannot happen
            raise DriveError(Reason.NO_ACCESS)
        items, folders = self._walk(tree, folder_id)
        return Listing(root, folders, items, 0)

    @staticmethod
    def _walk(tree: recordings.RecordedTree, start: str) -> tuple[tuple[DriveItem, ...], int]:
        found: list[DriveItem] = []
        folders = 0
        queue = deque([start])
        while queue:
            current = queue.popleft()
            folders += 1
            found.extend(tree.items[current])
            queue.extend(tree.children[current])
        return tuple(found), folders

    def iter_download(self, file_id: str, *, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        item = self._item(file_id)
        self._sleep(self._delay)
        data = TINY_PDF if item.mime_type == "application/pdf" else (b"fake drive file " + item.id.encode())
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    def read_google(self, file_id: str, mime_type: str) -> dict[str, Any]:
        self._item(file_id)
        name = _SAMPLE_OF.get(mime_type)
        if name is None:
            raise DriveError(Reason.UNKNOWN_ERROR)
        self._sleep(self._delay)
        try:
            if self._samples is None:
                folder: Traversable | Path = files("drive.convert").joinpath("samples")
            else:
                folder = self._samples
            raw = folder.joinpath(name).read_text("utf-8")
            return copy.deepcopy(json.loads(raw))
        except (OSError, ValueError):
            raise DriveError(Reason.UNKNOWN_ERROR) from None

    # -- helpers -----------------------------------------------------------
    def _special(self, folder_id: str) -> DriveFolder | None:
        names = {
            EMPTY_FOLDER: "Empty folder",
            FORMS_ONLY: "Forms only",
            IMPORT_FAILS: "Import fails",
        }
        if folder_id in names:
            return DriveFolder(folder_id, names[folder_id], in_shared_drive=False)
        return None

    def _forms(self, folder_id: str) -> list[DriveItem]:
        return [
            DriveItem(
                id=f"{folder_id}-i{n}",
                name=f"Other Google files {n}",
                mime_type=GOOGLE + "form",
                parent_id=folder_id,
                size=None,
                modified_time=None,
                version=1,
                md5=None,
            )
            for n in (1, 2)
        ]

    def _tree_of(self, some_id: str) -> recordings.RecordedTree | None:
        rec = recordings.find_recording(self._fixtures, some_id)
        if rec is None:
            return None
        if rec not in self._trees:
            self._trees[rec] = recordings.load(self._fixtures, rec)
        return self._trees[rec]

    def _item(self, file_id: str) -> DriveItem:
        if file_id.startswith(FORMS_ONLY + "-i"):
            for item in self._forms(FORMS_ONLY):
                if item.id == file_id:
                    return item
        tree = self._tree_of(file_id)
        if tree is None or file_id not in tree.by_id:
            raise DriveError(Reason.NO_ACCESS)
        return tree.by_id[file_id]
