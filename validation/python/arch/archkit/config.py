"""Load the adopter's `archkit.toml` and resolve the names and files it gives.

Every project fact a check needs (the app factory, operation lists, the error-code
catalogue, the snapshot path) comes from this file, never from the kit. A table or key the
kit does not read is an error, and `archkit run` requires every check's table."""

from __future__ import annotations

import importlib
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """The configuration is malformed or names something that does not exist."""


#: The shared tables and the keys each accepts; `[archkit.hermetic]` is a sub-table of
#: `[archkit]` with its own keys. Neither is a check.
SHARED_SECTIONS: dict[str, frozenset[str]] = {
    "archkit": frozenset(
        {"project_root", "python_path", "app_factory", "filterwarnings", "hermetic"}
    ),
    "http": frozenset(
        {
            "path_params",
            "default_path_param",
            "boom_path",
            "envelope_code",
            "envelope_message",
            "probe_headers",
        }
    ),
}
HERMETIC_KEYS = frozenset({"settings_class", "cache_clear", "env"})

#: Every check the kit runs (`archkit/checks/test_<section>.py`) and the keys its table
#: accepts. Each one is required: `archkit run` refuses a configuration that leaves one out.
CHECK_SECTIONS: dict[str, frozenset[str]] = {
    "error_codes": frozenset({"codes", "codes_file", "probes", "ignore_handlers"}),
    "error_exposure": frozenset({"unknown_path", "method_not_allowed", "leak_markers"}),
    "request_id": frozenset(
        {"header", "ok_path", "unknown_path", "method_not_allowed", "unauthenticated_path"}
    ),
    "route_contract": frozenset(
        {"public_operations", "create_operation_prefix", "request_headers"}
    ),
    "openapi_snapshot": frozenset({"path", "regenerate"}),
}

#: Keys without a default: `archkit run` refuses a configuration that leaves one out.
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "archkit": ("app_factory",),
    "error_codes": ("probes",),
    "error_exposure": ("method_not_allowed",),
    "request_id": ("ok_path", "method_not_allowed", "unauthenticated_path"),
    "openapi_snapshot": ("path",),
}


@dataclass(frozen=True)
class Config:
    """One parsed configuration file; `root` is the project every relative path names."""

    path: Path
    root: Path
    data: dict[str, Any]

    def section(self, name: str) -> dict[str, Any] | None:
        """The `[name]` table, or `None` when the file does not contain it."""
        value = self.data.get(name)
        if value is None:
            return None
        if not isinstance(value, dict):
            msg = f"[{name}] in {self.path} must be a table"
            raise ConfigError(msg)
        return value

    @property
    def settings(self) -> dict[str, Any]:
        """The `[archkit]` table (project root, python path, app factory, hermetic env)."""
        return self.section("archkit") or {}

    def resolve_path(self, value: str) -> Path:
        """`value` as an absolute path; a relative one is relative to the project root."""
        path = Path(value)
        return path if path.is_absolute() else (self.root / path).resolve()

    def python_path(self) -> list[str]:
        """The `[archkit] python_path` entries, absolute, in the order given."""
        return [str(self.resolve_path(entry)) for entry in self.settings.get("python_path", [])]


def load_config(path: Path) -> Config:
    """Parse `path`; `[archkit] project_root` is relative to the file's own folder."""
    resolved = path.resolve()
    if not resolved.is_file():
        msg = f"archkit configuration not found: {resolved}"
        raise ConfigError(msg)
    with resolved.open("rb") as handle:
        data = tomllib.load(handle)
    _check_tables(resolved, data)
    root_value = data.get("archkit", {}).get("project_root", ".")
    root = (resolved.parent / root_value).resolve()
    return Config(path=resolved, root=root, data=data)


def _unknown_keys(path: Path, where: str, table: dict[str, Any], known: frozenset[str]) -> None:
    unknown = sorted(set(table) - known)
    if unknown:
        msg = f"{path}: unknown key(s) {unknown} in [{where}] (it accepts {sorted(known)})"
        raise ConfigError(msg)


def _check_tables(path: Path, data: dict[str, Any]) -> None:
    """Refuse a table or key the kit does not read, so a typo or a retired check is loud."""
    known = {**SHARED_SECTIONS, **CHECK_SECTIONS}
    unknown = sorted(set(data) - set(known))
    if unknown:
        msg = (
            f"{path}: unknown section(s) {unknown}; archkit reads only {sorted(known)} "
            "(remove the section)"
        )
        raise ConfigError(msg)
    for name, table in data.items():
        if not isinstance(table, dict):
            msg = f"[{name}] in {path} must be a table"
            raise ConfigError(msg)
        _unknown_keys(path, name, table, known[name])
    hermetic = data.get("archkit", {}).get("hermetic", {})
    if not isinstance(hermetic, dict):
        msg = f"[archkit.hermetic] in {path} must be a table"
        raise ConfigError(msg)
    _unknown_keys(path, "archkit.hermetic", hermetic, HERMETIC_KEYS)


def require_checks(config: Config) -> None:
    """Refuse a configuration that leaves out a check section or a key without a default."""
    missing = [name for name in CHECK_SECTIONS if config.section(name) is None]
    if missing:
        msg = f"{config.path}: missing check section(s) {missing}; every check is required"
        raise ConfigError(msg)
    absent = [
        f"[{name}] {key}"
        for name, keys in REQUIRED_KEYS.items()
        for key in keys
        if key not in (config.section(name) or {})
    ]
    if absent:
        msg = f"{config.path}: required key(s) not set: {absent}"
        raise ConfigError(msg)


def install_python_path(config: Config) -> None:
    """Prepend the configured import roots to `sys.path` so the project imports by name."""
    for entry in reversed(config.python_path()):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def resolve(reference: str) -> Any:
    """Import `package.module:attribute.sub` and return the named object."""
    module_name, separator, attribute_path = reference.partition(":")
    if not separator or not module_name or not attribute_path:
        msg = f"{reference!r} is not a 'package.module:attribute' reference"
        raise ConfigError(msg)
    target: Any = importlib.import_module(module_name)
    for part in attribute_path.split("."):
        target = getattr(target, part)
    return target


def read_data_file(path: Path) -> Any:
    """A JSON, YAML or TOML data file, chosen by its suffix."""
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    if suffix in {".yml", ".yaml"}:
        return yaml.safe_load(text)
    if suffix == ".toml":
        return tomllib.loads(text)
    msg = f"{path}: unsupported data file type (use .json, .yaml, .yml or .toml)"
    raise ConfigError(msg)


def load_records(config: Config, table: dict[str, Any], key: str) -> list[Any]:
    """The list under `key`, given inline or in the data file named by `<key>_file`.

    A data file holds the list itself, or a table whose `key` entry is the list; this is
    how a catalogue built from the project's own documents reaches a check."""
    if key in table:
        value = table[key]
    elif f"{key}_file" in table:
        loaded = read_data_file(config.resolve_path(table[f"{key}_file"]))
        value = loaded.get(key) if isinstance(loaded, dict) else loaded
    else:
        msg = f"{config.path}: neither '{key}' nor '{key}_file' is set"
        raise ConfigError(msg)
    if not isinstance(value, list):
        msg = f"{config.path}: '{key}' must be a list"
        raise ConfigError(msg)
    return value


def parse_operation(spec: str) -> tuple[str, str]:
    """`"POST /api/orders"` -> `("post", "/api/orders")`."""
    method, _, path = spec.strip().partition(" ")
    if not method or not path.strip().startswith("/"):
        msg = f"operation {spec!r} is not written as 'METHOD /path'"
        raise ConfigError(msg)
    return method.lower(), path.strip()
