"""Inputs and results of the Drive import (INT-GDUI-0004).

Workflow-sandbox safe (pydantic, uuid, typing only). Everything here is an id or a
fixed code: never a file name, folder name, link or Google text (history rule).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

ItemKind = Literal["pdf", "google", "stored"]


class DriveImportInput(BaseModel):
    project_id: UUID
    connection_id: UUID
    #: Workflow-id prefix of tier-2 tests (empty in production); child parse ids carry it.
    id_prefix: str = Field(default="", max_length=64)
    #: Set by continue-as-new: the listing is already stored.
    listed: bool = False
    #: Epoch seconds of the first run; the soft deadline and the duration count from it.
    started_epoch: float | None = None
    #: Continue-as-new after this many batches in one run (tests; ``None`` = only when the server suggests it).
    max_batches_per_run: int | None = Field(default=None, ge=1)


class ListResult(BaseModel):
    #: ``listed`` go on; ``stop`` end quietly (connection gone or not open); ``failed`` already recorded.
    outcome: Literal["listed", "stop", "failed"]
    files: int = 0


class BatchRequest(BaseModel):
    project_id: UUID
    connection_id: UUID


class BatchItem(BaseModel):
    item_id: UUID
    kind: ItemKind


class BatchResult(BaseModel):
    #: Pending files read; 0 = the import is done. Row-only files are finished here.
    pending: int = 0
    stop: bool = False
    items: list[BatchItem] = Field(default_factory=list)


class ImportItemInput(BaseModel):
    project_id: UUID
    connection_id: UUID
    item_id: UUID
    kind: ItemKind


class ImportItemResult(BaseModel):
    #: ``done`` the file is finished; ``parse`` the workflow runs the child parse, then finishes it;
    #: ``wait`` the file links to a document another flow still parses: wait for its final status.
    action: Literal["done", "parse", "wait"]
    document_id: UUID | None = None


class FinishItemInput(BaseModel):
    project_id: UUID
    connection_id: UUID
    item_id: UUID
    state: Literal["processed", "error"]
    document_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=40)
    step: str | None = Field(default=None, max_length=20)


class WaitDocumentInput(BaseModel):
    project_id: UUID
    document_id: UUID


class WaitDocumentResult(BaseModel):
    #: ``parsed``, ``failed`` or ``timeout`` (still not final after the parse limit).
    status: Literal["parsed", "failed", "timeout"]


class CompleteInput(BaseModel):
    project_id: UUID
    connection_id: UUID
    started_epoch: float | None = None


class FailImportInput(BaseModel):
    project_id: UUID
    connection_id: UUID
    reason: str = Field(default="unknown_error", max_length=40)


class ImportEnd(BaseModel):
    """Workflow result: the connection state the run left."""

    state: str
