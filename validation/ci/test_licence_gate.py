#!/usr/bin/env python3
"""Self-tests of licence_gate.py: `env -u VIRTUAL_ENV uv run --project validation/python/lint pytest validation/ci`.

No uv, no network: the environments and the pip-licenses listing are replaced by recorded rows. The
real backend/licence-gate.toml, backend/pyproject.toml and NOTICE are read, so the config and NOTICE
stay consistent with the dependency declarations.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import licence_gate
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = ROOT / "backend" / "licence-gate.toml"


@pytest.fixture(scope="module")
def config() -> licence_gate.Config:
    return licence_gate.Config(CONFIG, tomllib.loads(CONFIG.read_text(encoding="utf-8")))


def row(name: str, licence: str, version: str = "1.0") -> dict[str, str]:
    return {"Name": name, "Version": version, "License": licence}


def pinned_rows(config: licence_gate.Config) -> list[dict[str, str]]:
    """The overridden packages at their pinned versions, as pip-licenses would list them."""
    return [row(name, "UNKNOWN", version) for name, version in config.policy.override_versions.items()]


@pytest.mark.parametrize(
    "licence",
    [
        "MIT",
        "MIT License",
        "Apache Software License; BSD License",
        "Apache-2.0 OR BSD-3-Clause",
        "BSD 3-Clause OR Apache-2.0",
        "MIT AND PSF-2.0",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "MPL-2.0 AND MIT",
        "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        "Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND ISC AND MIT AND Zlib",
        "(MIT OR Apache-2.0) AND BSD-3-Clause",
        "ISC License (ISCL)",
    ],
)
def test_licences_of_the_current_trees_pass(config: licence_gate.Config, licence: str) -> None:
    assert licence_gate.check_licence(config.policy, "pkg", licence) == []


@pytest.mark.parametrize(
    ("licence", "bad"),
    [
        ("AGPL-3.0-or-later", ["AGPL-3.0-or-later"]),
        ("GNU Lesser General Public License v2 (LGPLv2)", ["GNU Lesser General Public License v2 (LGPLv2)"]),
        ("UNKNOWN", ["UNKNOWN"]),
        ("MIT AND GPL-3.0-only", ["GPL-3.0-only"]),
        ("GPL-2.0-only OR LGPL-2.1-only", ["GPL-2.0-only", "LGPL-2.1-only"]),
        ("Apache-2.0 WITH Classpath-exception-2.0", ["Apache-2.0 WITH Classpath-exception-2.0"]),
    ],
)
def test_a_licence_off_the_allowlist_is_named(config: licence_gate.Config, licence: str, bad: list[str]) -> None:
    assert licence_gate.check_licence(config.policy, "pkg", licence) == bad


def test_an_alternative_that_is_allowed_passes_an_or(config: licence_gate.Config) -> None:
    assert licence_gate.check_licence(config.policy, "pkg", "GPL-3.0-only OR MIT") == []


def test_an_unreadable_expression_is_a_finding(config: licence_gate.Config) -> None:
    assert licence_gate.check_licence(config.policy, "pkg", "(MIT AND") == ["(MIT AND"]


def test_an_override_replaces_the_reported_licence(config: licence_gate.Config) -> None:
    assert licence_gate.check_listing(config.policy, "api", [row("dotenv", "UNKNOWN"), row("Pypdfium2", "BSD-3-Clause, Apache-2.0, dependency licenses")]) == []


@pytest.mark.parametrize("name", ["pymupdf", "PyMuPDF4LLM", "pymupdf_layout"])
def test_a_pymupdf_package_fails_in_any_licence(config: licence_gate.Config, name: str) -> None:
    findings = licence_gate.check_listing(config.policy, "worker-parse", [row(name, "MIT")])
    assert len(findings) == 1
    assert "forbidden" in findings[0]
    assert findings[0].startswith("worker-parse: ")


def test_a_clean_listing_has_no_findings(config: licence_gate.Config) -> None:
    assert licence_gate.check_listing(config.policy, "api", [row("fastapi", "MIT"), row("certifi", "Mozilla Public License 2.0 (MPL 2.0)")]) == []


def test_the_lockfile_holds_no_forbidden_package(config: licence_gate.Config) -> None:
    names = licence_gate.lockfile_names(config.resolve("project") / config.data["lockfile"])
    assert "docling-slim" in names
    assert not names & config.policy.forbidden


def test_notice_names_every_required_component(config: licence_gate.Config) -> None:
    """The config list plus the direct dependencies of the parse extra, against the real NOTICE (no install)."""
    notice = config.data["notice"]
    pyproject = tomllib.loads((config.resolve("project") / "pyproject.toml").read_text(encoding="utf-8"))
    direct = licence_gate.requirement_names(pyproject["project"]["optional-dependencies"][notice["extra"]])
    packages = sorted({*notice["packages"], *direct})
    tree = {licence_gate.normalise(p) for p in packages}
    text = config.resolve("notice-file").read_text(encoding="utf-8")
    assert licence_gate.notice_findings(text, tree, packages, notice["text"]) == []
    assert not any(licence_gate.normalise(f) in licence_gate.normalise(text) for f in config.policy.forbidden), "PyMuPDF is still named in NOTICE"


def test_an_override_must_match_an_installed_package_at_its_pinned_version(config: licence_gate.Config) -> None:
    pinned = pinned_rows(config)
    assert licence_gate.override_findings(config.policy, {"api": pinned}) == []
    moved = [{**r, "Version": "99"} for r in pinned]
    findings = licence_gate.override_findings(config.policy, {"api": moved})
    assert len(findings) == len(pinned)
    assert all("is pinned to" in f and "99 is installed" in f for f in findings)
    gone = licence_gate.override_findings(config.policy, {"api": pinned[1:]})
    assert gone == [f"override {licence_gate.normalise(pinned[0]['Name'])} matches no installed package: drop it from the config"]


DOCKERFILES = {"api": "api.Dockerfile", "worker-agents": "worker.Dockerfile", "worker-parse": "worker-parse.Dockerfile"}


@pytest.mark.parametrize("role", DOCKERFILES)
def test_the_role_extras_equal_what_its_dockerfile_installs(config: licence_gate.Config, role: str) -> None:
    """Every `uv sync ... --extra X` line of the image's Dockerfile installs the extras the config names for the role."""
    text = (ROOT / "backend" / DOCKERFILES[role]).read_text(encoding="utf-8")
    syncs = [line for line in text.splitlines() if "uv sync" in line]
    assert syncs, f"{DOCKERFILES[role]} has no uv sync line"
    declared = next(r["extras"] for r in config.data["roles"] if r["name"] == role)
    for line in syncs:
        assert re.findall(r"--extra (\S+)", line) == declared, line
    assert set(DOCKERFILES) == {r["name"] for r in config.data["roles"]}


def test_notice_findings_list_the_missing_names() -> None:
    findings = licence_gate.notice_findings("Docling and torch", {"docling", "torch", "lxml"}, ["docling", "torch", "lxml", "ghost"], ["weights"])
    assert findings == [
        "NOTICE does not name lxml (a dependency of the parse set)",
        "NOTICE does not name ghost (a dependency of the parse set)",
        "NOTICE does not mention 'weights'",
        "notice package ghost is not in the worker-parse tree: drop it from the config",
    ]


def test_a_package_that_ships_a_notice_file_must_be_named_in_notice() -> None:
    listing = [
        {"Name": "botocore", "Version": "1", "License": "Apache-2.0", "NoticeText": "Botocore\nCopyright Amazon"},
        {"Name": "requests", "Version": "1", "License": "Apache-2.0", "NoticeText": "Requests"},
        {"Name": "attrs", "Version": "1", "License": "MIT", "NoticeText": "UNKNOWN"},
        {"Name": "idna", "Version": "1", "License": "BSD-3-Clause"},
    ]
    names = licence_gate.packages_with_notice(listing)
    assert names == {"botocore", "requests"}
    assert licence_gate.shipped_notice_findings("boto3, botocore", names) == [
        "NOTICE does not name requests (its distribution ships a NOTICE file)"
    ]
    assert licence_gate.shipped_notice_findings("botocore and Requests", names) == []


def test_notice_names_match_on_word_boundaries() -> None:
    assert licence_gate.notice_findings("python-docx and torchvision", set(), [], []) == []
    assert licence_gate.notice_findings("torchvision", {"torch"}, ["torch"], []) == ["NOTICE does not name torch (a dependency of the parse set)"]


def test_main_fails_when_a_role_holds_pymupdf(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Scenario: api and worker-agents are clean, worker-parse gains pymupdf4llm and an AGPL package -> exit 1, both named.

    gate -> uv sync (stubbed) -> pip-licenses (stubbed rows) -> allowlist and forbidden checks -> findings on stderr
    """
    config = licence_gate.Config(CONFIG, tomllib.loads(CONFIG.read_text(encoding="utf-8")))
    clean = [row("fastapi", "MIT"), *pinned_rows(config)]
    notice = config.data["notice"]
    pyproject = tomllib.loads((config.resolve("project") / "pyproject.toml").read_text(encoding="utf-8"))
    names = {*notice["packages"], *licence_gate.requirement_names(pyproject["project"]["optional-dependencies"]["parse"])}
    parse_tree = [row(n, "MIT") for n in names if licence_gate.normalise(n) not in config.policy.override_versions]
    parse_tree += pinned_rows(config)
    listings = {"gcp-aws": clean, "gcp-aws-parse": [*parse_tree, row("pymupdf4llm", "AGPL-3.0-or-later"), row("somelib", "AGPL-3.0-only")]}
    monkeypatch.setattr(licence_gate, "sync_env", lambda _project, extras, target: Path("-".join(extras)))
    monkeypatch.setattr(licence_gate, "list_licences", lambda _pip, python: listings[python.name])

    assert licence_gate.main(["--config", str(CONFIG), "--workdir", "unused"]) == licence_gate.EXIT_FINDINGS

    err = capsys.readouterr().err
    assert "worker-parse: pymupdf4llm 1.0 is forbidden" in err
    assert "worker-parse: somelib 1.0: licence 'AGPL-3.0-only' is not on the allowlist" in err
    assert "api:" not in err


def test_main_is_clean_for_allowed_trees(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = licence_gate.Config(CONFIG, tomllib.loads(CONFIG.read_text(encoding="utf-8")))
    pyproject = tomllib.loads((config.resolve("project") / "pyproject.toml").read_text(encoding="utf-8"))
    names = {*config.data["notice"]["packages"], *licence_gate.requirement_names(pyproject["project"]["optional-dependencies"]["parse"])}
    rows = [row(n, "MIT") for n in names if licence_gate.normalise(n) not in config.policy.override_versions]
    rows += pinned_rows(config)
    monkeypatch.setattr(licence_gate, "sync_env", lambda _project, extras, target: Path("-".join(extras)))
    monkeypatch.setattr(licence_gate, "list_licences", lambda _pip, _python: rows)

    assert licence_gate.main(["--config", str(CONFIG), "--workdir", "unused"]) == licence_gate.EXIT_OK
    assert "clean" in capsys.readouterr().out


def test_main_reports_a_tool_error(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad = tmp_path / "gate.toml"
    bad.write_text('forbidden = ["x"]\n', encoding="utf-8")
    assert licence_gate.main(["--config", str(bad)]) == licence_gate.EXIT_TOOL_ERROR
    assert "missing key 'allowed'" in capsys.readouterr().err


def test_main_rejects_an_unknown_role_in_env(capsys: pytest.CaptureFixture[str]) -> None:
    assert licence_gate.main(["--config", str(CONFIG), "--env", "nope=/bin/python3"]) == licence_gate.EXIT_TOOL_ERROR
    assert "unknown roles: nope" in capsys.readouterr().err
