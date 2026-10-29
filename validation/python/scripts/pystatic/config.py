"""The adopter's configuration: one TOML file, one table per configurable check.

`--config PATH` names it; otherwise `<root>/pystatic.toml` is read when it exists. A check
whose table is absent either runs on its defaults or reports itself skipped (never a silent
pass). `pystatic.example.toml` documents every table."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pystatic import files

__all__ = [
    "CONFIG_NAME",
    "ConfigError",
    "config_path",
    "configure",
    "has_section",
    "section",
    "string_list",
]

CONFIG_NAME = "pystatic.toml"


class ConfigError(ValueError):
    """The configuration file exists but cannot be read, or a value has the wrong shape."""


@dataclass
class _State:
    path: Path | None = None


_STATE = _State()


def configure(path: Path | None) -> None:
    """Use `path` (relative paths resolve against the working directory) for later reads."""
    _STATE.path = path.resolve() if path is not None else None


def config_path() -> Path:
    return _STATE.path if _STATE.path is not None else files.target_root() / CONFIG_NAME


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        if _STATE.path is not None:
            msg = f"configuration file {path} does not exist"
            raise ConfigError(msg)
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path}: {exc}"
        raise ConfigError(msg) from exc


def has_section(name: str) -> bool:
    return name in _load(config_path())


def section(name: str) -> Any:
    """The `[name]` table (a dict) or `[[name]]` array (a list); `{}` when absent."""
    return _load(config_path()).get(name, {})


def string_list(table: dict[str, Any], key: str, default: list[str]) -> list[str]:
    """`table[key]` as a list of strings, or `default` when the key is absent."""
    value = table.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{config_path().name}: `{key}` must be a list of strings"
        raise ConfigError(msg)
    return list(value)
