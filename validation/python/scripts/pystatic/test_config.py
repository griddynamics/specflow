"""Self-test for `config` and for the shipped `pystatic.example.toml`: it parses, and every
check accepts its table."""

from __future__ import annotations

from pathlib import Path

import pytest

from pystatic import config, files
from pystatic.docs import settings
from pystatic.scripts import (
    clones,
    config_literals,
    deployment_files,
    json_schemas,
    package_data,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "pystatic.example.toml"


@pytest.fixture
def example(tmp_path: Path) -> None:
    files.configure(tmp_path)
    config.configure(EXAMPLE)


def test_absent_default_file_reads_as_empty(tmp_path: Path) -> None:
    files.configure(tmp_path)
    assert config.section("anything") == {}
    assert config.has_section("anything") is False


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    config.configure(tmp_path / "absent.toml")
    with pytest.raises(config.ConfigError, match="does not exist"):
        config.section("anything")


def test_malformed_file_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "pystatic.toml").write_text("[broken\n")
    files.configure(tmp_path)
    with pytest.raises(config.ConfigError):
        config.section("anything")


def test_string_list_refuses_other_shapes() -> None:
    with pytest.raises(config.ConfigError, match="list of strings"):
        config.string_list({"scan": "src"}, "scan", [])


@pytest.mark.usefixtures("example")
def test_the_example_configures_every_table() -> None:
    for name in (
        "no-dict-schemas",
        "enum-literals",
        "config-literals",
        "registry-files",
        "json-schemas",
        "deployment-files",
        "file-size",
        "source-ownership",
        "runtime-defaults",
        "package-data",
        "docs-bundle",
        "clones",
    ):
        assert config.section(name), name


@pytest.mark.usefixtures("example")
def test_the_example_tables_parse() -> None:
    bundle = settings.load()
    assert bundle.folders[0].path == "guidelines"
    assert bundle.numbered_records[0].filename.match("adr-07-use-queues.md")
    assert config_literals._settings(config.section("config-literals")).config_classes
    assert deployment_files._policy(config.section("deployment-files")).allowed_keys
    assert json_schemas.entries()[0].name_field == "kind"
    assert package_data._settings() == ("src", (".sql", ".cypher", ".json"))
    assert clones.settings(config.section("clones")).tests == ("tests/*",)
