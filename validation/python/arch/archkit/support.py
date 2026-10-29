"""Helpers the bundled checks share: section lookup, the OpenAPI walk and HTTP probes."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import httpx

from archkit.config import Config, ConfigError

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
DEFAULT_PATH_PARAM = "test-id-000"
_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")


def section(config: Config, name: str) -> dict[str, Any]:
    """The `[name]` table of a check; `archkit run` refuses a configuration without it."""
    table = config.section(name)
    if table is None:
        msg = f"{config.path}: [{name}] is required"
        raise ConfigError(msg)
    return table


def http_table(config: Config) -> dict[str, Any]:
    """The shared `[http]` table: path dummies, error-envelope paths, the boom route."""
    return config.section("http") or {}


def probe_headers(config: Config) -> dict[str, str]:
    """Headers every probe request carries (`[http] probe_headers`; none by default)."""
    headers = http_table(config).get("probe_headers", {})
    return {str(key): str(value) for key, value in headers.items()}


def iter_operations(openapi_schema: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """`(method, path, operation)` for every HTTP-verb operation of an OpenAPI document."""
    for path, operations in openapi_schema.get("paths", {}).items():
        for method, operation in operations.items():
            if method in HTTP_METHODS:
                yield method, path, operation


def fill_path(path: str, dummies: dict[str, str], default: str = DEFAULT_PATH_PARAM) -> str:
    """Replace every `{name}` of an OpenAPI path template with a dummy value."""
    return _PATH_PARAM_RE.sub(lambda match: str(dummies.get(match.group(1), default)), path)


def fill(config: Config, path: str) -> str:
    """`fill_path` with the `[http] path_params` and `default_path_param` of the config."""
    table = http_table(config)
    return fill_path(
        path, table.get("path_params", {}), table.get("default_path_param", DEFAULT_PATH_PARAM)
    )


def dotted_get(value: Any, dotted: str) -> Any:
    """`value["a"]["b"]` for `"a.b"`, or `None` when any step is missing."""
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def json_body(response: httpx.Response) -> Any:
    """The parsed JSON body, or `None` when the body is not JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def envelope_code(config: Config, response: httpx.Response) -> Any:
    """The error code of an error response, read at `[http] envelope_code`."""
    return dotted_get(json_body(response), http_table(config).get("envelope_code", "error.code"))
