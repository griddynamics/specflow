"""Build options, settings and the build context."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from graph.build.option_models import BuildOptions, BuildSettings, FragmentTokens, Locality
from graph.build.ports import BuildStore, CorpusSource, ExtractionStore
from graph.core.identity import SpecIdRegistry, validate_project_id
from graph.core.llm import Gateway
from graph.core.ontology import Ontology
from graph.core.store import Executor, as_executor

__all__ = ["BuildContext", "BuildOptions", "BuildSettings", "FragmentTokens", "Locality", "StepServices"]


@dataclass(frozen=True, slots=True)
class StepServices:
    """The model gateway, ontology and build options one step function works with."""

    gateway: Gateway
    ontology: Ontology
    options: BuildOptions


@dataclass(frozen=True, slots=True)
class BuildContext:
    """Everything a build step needs (api.md "Build"); holds live objects, so it is never serialised.

    ``build_id`` None starts a new build; set, the steps read and resume that build. ``driver`` is the
    ``GraphStore`` of the build (a Neo4j driver still works, deprecated; None when ``executor`` is
    given). ``executor`` None is replaced by the store's executor (for a driver: the Neo4j store's, on
    ``database``); a test may pass its own.
    """

    project_id: str
    ontology: Ontology
    driver: Any
    gateway: Gateway
    registry: SpecIdRegistry
    corpus: CorpusSource
    extraction: ExtractionStore
    builds: BuildStore
    rebuild: bool = False
    options: BuildOptions = field(default_factory=BuildOptions)
    settings: BuildSettings = field(default_factory=BuildSettings)
    retry_lineages: tuple[str, ...] = ()
    build_id: str | None = None
    database: str | None = None
    executor: Executor | None = None

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        object.__setattr__(self, "retry_lineages", tuple(dict.fromkeys(self.retry_lineages)))
        if self.executor is None:
            object.__setattr__(self, "executor", as_executor(self.driver, self.database))

    def with_build_id(self, build_id: str) -> BuildContext:
        """The same context bound to ``build_id`` (the executor is shared)."""

        return dataclasses.replace(self, build_id=build_id)
