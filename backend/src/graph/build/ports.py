"""Ports of the build: corpus, reusable extraction results, build records and artifacts (api.md "Ports").

State outside Neo4j goes through these async ports; ``open_file_stores`` returns file implementations
and a host may supply its own. Payloads are JSON objects (``model_dump(mode="json")`` of a model),
so an implementation never needs to know the build's models. Backend failures raise
``InfrastructureUnavailableError``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final, Literal, Protocol

from pydantic import AwareDatetime, Field, ValidationError

from graph.build.models import ArtifactModel, BuildMode, BuildResult, BuildStatus, StepName
from graph.build.option_models import BuildOptions
from graph.core.errors import CorpusError

ExtractionKind = Literal["nodes", "edges", "vectors", "attributes", "decisions"]
EXTRACTION_KINDS: Final[tuple[ExtractionKind, ...]] = ("nodes", "edges", "vectors", "attributes", "decisions")
RecordStatus = Literal["running"] | BuildStatus
StepState = Literal["running", "done", "failed"]
JsonObject = dict[str, Any]


class CorpusRevision(ArtifactModel):
    """Plan (build.md step 1.1): the current revision of one active lineage; invalid → ``CorpusError``."""

    lineage_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    version: int = Field(ge=0)
    path: str
    title: str
    markdown: str

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as error:
            fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
            raise CorpusError("corpus_invalid_revision", fields=",".join(fields)) from None


class CorpusSource(Protocol):
    """Port: the corpus snapshot of a build."""

    async def snapshot(self, project_id: str, *, dry_run: bool = False) -> list[CorpusRevision]:
        """Every active lineage's current revision in document order; raises ``CorpusError`` on a broken contract.

        ``dry_run`` reads the same revisions without recording anything (no version index update)."""
        ...


class ExtractionStore(Protocol):
    """Port: extraction results reusable by later builds (build.md "Reuse in update builds")."""

    async def get(self, kind: ExtractionKind, key: str, fingerprint: str) -> JsonObject | None:
        """The payload stored under ``kind`` and ``key``; None when absent or stored with another fingerprint."""
        ...

    async def put(self, kind: ExtractionKind, key: str, fingerprint: str, payload: JsonObject) -> None:
        """Store ``payload`` under ``kind`` and ``key``, replacing any earlier entry."""
        ...


class BatchExtractionStore(ExtractionStore, Protocol):
    """Optional extension of ``ExtractionStore``: batch reads and writes.

    The build calls these when a store has them (``graph.build.vectors.get_many``/``put_many``) and
    falls back to one ``get``/``put`` per key otherwise, so a host store needs only the two methods
    above. ``FileExtractionStore`` implements both.
    """

    async def get_many(self, kind: ExtractionKind, keys: Sequence[str], fingerprint: str) -> dict[str, JsonObject]:
        """``get`` of every key: payloads by key, absent (or other fingerprint) keys left out."""
        ...

    async def put_many(self, kind: ExtractionKind, fingerprint: str, entries: Mapping[str, JsonObject]) -> None:
        """``put`` of every entry (payload by key)."""
        ...


class BuildRecord(ArtifactModel):
    """Run (build.md "Modes"): one build: mode, frozen options and ontology shape, status, step progress."""

    build_id: str = Field(min_length=1)
    project_id: str
    mode: BuildMode
    options: BuildOptions
    ontology_fingerprint: str
    label_set: tuple[str, ...]
    relation_set: tuple[str, ...] | None = None
    """Relation names of the ontology; None in records written before it was recorded (not checked)."""
    embedding_model: str
    status: RecordStatus = "running"
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    progress: dict[StepName, StepState] = Field(default_factory=dict)

    @property
    def finished(self) -> bool:
        """``finish_build`` ran with a status other than ``failed``."""

        return self.finished_at is not None and self.status != "failed"


class BuildStore(Protocol):
    """Port: build records, per-build step artifacts and build results."""

    async def create_build(self, record: BuildRecord) -> None:
        """Store a new build record; its ``build_id`` is new in the project."""
        ...

    async def latest_build(
        self,
        project_id: str,
        *,
        finished_only: bool = False,
        modes: Collection[BuildMode] | None = None,
    ) -> BuildRecord | None:
        """The most recently started build of the project, of one of ``modes`` (None: any); with
        ``finished_only``, only builds whose record is ``finished``."""
        ...

    async def get_build(self, project_id: str, build_id: str) -> BuildRecord | None:
        """One build record."""
        ...

    async def put_progress(self, project_id: str, build_id: str, step: StepName, state: StepState) -> None:
        """Set one step's state in the build's ``progress``."""
        ...

    async def finish_build(self, result: BuildResult) -> None:
        """Store the result and set the record's ``status`` and ``finished_at`` from it."""
        ...

    async def get_result(self, project_id: str, build_id: str) -> BuildResult | None:
        """The result ``finish_build`` stored; None before the build finished."""
        ...

    async def put_artifact(self, project_id: str, build_id: str, step: StepName, payload: JsonObject) -> None:
        """Store the artifact of one step of a build, replacing an earlier one."""
        ...

    async def get_artifact(self, project_id: str, build_id: str, step: StepName) -> JsonObject | None:
        """The artifact of one step of a build; None when the step has not stored one."""
        ...
