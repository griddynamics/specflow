"""The generated runtime defaults that tests read instead of hard-coding them (no tests in here).

``fixtures/runtime-defaults.json`` holds ``SETTING_NAME: number`` for every numeric default the
backend declares: the process settings in ``settings.py`` (keyed by their environment variable)
and the graph build options in ``graph.build.option_models.BuildOptions`` (keyed by the upper-cased
option name, docs/graph/build.md "Build options"). pystatic's ``runtime-defaults`` check reads the
same file and reports a test that types one of these numbers next to the setting's name.
``test_runtime_defaults.py`` fails when the file drifts from the code; regenerate it from
``backend/`` with ``uv run python tests/runtime_defaults_support.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel

import settings
from graph.build.option_models import BuildOptions

DEFAULTS_FILE: Final = Path(__file__).resolve().parent / "fixtures" / "runtime-defaults.json"


def _numeric_defaults(model: type[BaseModel]) -> Iterator[tuple[str, int | float]]:
    """``(field alias or upper-cased name, default)`` of each field with a plain numeric default."""

    for name, info in model.model_fields.items():
        default = info.default
        if isinstance(default, bool) or not isinstance(default, int | float):  # required: PydanticUndefined
            continue
        alias = info.validation_alias
        yield (alias if isinstance(alias, str) else name.upper()), default


def _settings_models() -> list[type[BaseModel]]:
    """Every settings binding of ``settings.py``, in declaration order."""

    return [
        value
        for value in vars(settings).values()
        if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == settings.__name__
    ]


def declared_defaults() -> dict[str, int | float]:
    """The numeric defaults the code declares, sorted by setting name."""

    found: dict[str, int | float] = {}
    for model in [*_settings_models(), BuildOptions]:
        found.update(_numeric_defaults(model))
    return dict(sorted(found.items()))


def render(defaults: Mapping[str, int | float]) -> str:
    """The committed file's text: sorted keys, two-space indent, trailing newline."""

    return json.dumps(dict(defaults), indent=2, sort_keys=True) + "\n"


def write() -> None:
    """Regenerate ``fixtures/runtime-defaults.json`` from the code."""

    DEFAULTS_FILE.write_text(render(declared_defaults()), encoding="utf-8")


RUNTIME_DEFAULTS: Final[Mapping[str, int | float]] = MappingProxyType(
    json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
)


if __name__ == "__main__":
    write()
