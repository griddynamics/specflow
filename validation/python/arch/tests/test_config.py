"""Self-tests of `archkit.config`: loading, references, catalogues and operation specs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archkit.config import (
    ConfigError,
    load_config,
    load_records,
    parse_operation,
    require_checks,
    resolve,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_paths_resolve_against_the_project_root(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    text = '[archkit]\nproject_root = "project"\npython_path = ["src"]\n'
    config = load_config(_write(tmp_path / "archkit.toml", text))
    assert config.root == (tmp_path / "project").resolve()
    assert config.python_path() == [str((tmp_path / "project" / "src").resolve())]
    assert config.section("missing") is None


def test_a_missing_configuration_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml")


def test_a_retired_or_unknown_section_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown section\(s\) \['csrf'\]"):
        load_config(_write(tmp_path / "archkit.toml", '[archkit]\n[csrf]\nheader = "X"\n'))


def test_an_unknown_key_is_an_error(tmp_path: Path) -> None:
    text = '[request_id]\npreflight_origin = "http://localhost"\n'
    with pytest.raises(
        ConfigError, match=r"unknown key\(s\) \['preflight_origin'\] in \[request_id\]"
    ):
        load_config(_write(tmp_path / "archkit.toml", text))
    hermetic = "[archkit.hermetic]\nsettings = 1\n"
    with pytest.raises(ConfigError, match=r"in \[archkit.hermetic\]"):
        load_config(_write(tmp_path / "archkit.toml", hermetic))


def test_a_run_requires_every_check_and_its_required_keys(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / "archkit.toml", "[archkit]\n[error_codes]\n"))
    with pytest.raises(ConfigError, match="missing check section"):
        require_checks(config)
    complete = (
        '[archkit]\napp_factory = "app:create"\n'
        "[error_codes]\nprobes = []\n"
        '[error_exposure]\nmethod_not_allowed = "DELETE /x"\n'
        '[request_id]\nok_path = "/"\nmethod_not_allowed = "DELETE /x"\n'
        "[route_contract]\n"
        '[openapi_snapshot]\npath = "openapi.json"\n'
    )
    config = load_config(_write(tmp_path / "archkit.toml", complete))
    with pytest.raises(ConfigError, match=r"\[request_id\] unauthenticated_path"):
        require_checks(config)
    fixed = complete.replace('ok_path = "/"', 'ok_path = "/"\nunauthenticated_path = "/me"')
    require_checks(load_config(_write(tmp_path / "archkit.toml", fixed)))


def test_resolve_walks_attributes() -> None:
    assert resolve("json:dumps") is json.dumps
    assert resolve("os.path:join.__name__") == "join"
    with pytest.raises(ConfigError):
        resolve("json.dumps")


def test_records_come_inline_or_from_a_data_file(tmp_path: Path) -> None:
    _write(tmp_path / "codes.json", json.dumps([{"code": "NOT_FOUND", "http": 404}]))
    _write(tmp_path / "codes.yaml", "codes:\n  - code: CONFLICT\n    http: 409\n")
    config = load_config(_write(tmp_path / "archkit.toml", "[archkit]\n"))
    assert load_records(config, {"codes": [1]}, "codes") == [1]
    assert load_records(config, {"codes_file": "codes.json"}, "codes") == [
        {"code": "NOT_FOUND", "http": 404}
    ]
    assert load_records(config, {"codes_file": "codes.yaml"}, "codes") == [
        {"code": "CONFLICT", "http": 409}
    ]
    with pytest.raises(ConfigError):
        load_records(config, {}, "codes")


def test_parse_operation() -> None:
    assert parse_operation("POST /api/orders") == ("post", "/api/orders")
    assert parse_operation(" get  /health ") == ("get", "/health")
    with pytest.raises(ConfigError):
        parse_operation("/api/orders")
