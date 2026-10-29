"""Self-tests of the kit's semgrep rules: `semgrep --test` passes with complete fixture
coverage, every rule id is `kit.`-namespaced, `paths:` scoping holds on a real scan, and the
in-line semgrep suppression comment is caught by a real scan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from tests.semgrep_support import (
    CONFIGURABLE_DIR,
    CONFIGURABLE_FIXTURES,
    FIXTURES,
    SEMGREP_DIR,
    assert_scoping,
    real_scan,
    run_semgrep,
)

RULE_FILES = sorted([*SEMGREP_DIR.glob("*.yml"), *CONFIGURABLE_DIR.glob("*.yml")])


def _rules(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text())
    rules = data["rules"]
    assert isinstance(rules, list)
    assert rules, f"{path.name} declares no rules"
    return rules


def test_semgrep_selftest_passes_with_complete_coverage() -> None:
    # `semgrep --test`'s exit code ignores a config with no fixtures; only its JSON
    # `config_missing_tests`/`config_with_errors` show that gap.
    proc = run_semgrep("--test", "--json", "--config", str(SEMGREP_DIR), str(FIXTURES))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["config_missing_tests"] == [], report["config_missing_tests"]
    assert report["config_with_errors"] == [], report["config_with_errors"]


def test_configurable_selftest_passes_with_complete_coverage() -> None:
    proc = run_semgrep(
        "--test", "--json", "--config", str(CONFIGURABLE_DIR), str(CONFIGURABLE_FIXTURES)
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["config_missing_tests"] == [], report["config_missing_tests"]
    assert report["config_with_errors"] == [], report["config_with_errors"]


def test_every_rule_id_is_namespaced_and_unique() -> None:
    seen: set[str] = set()
    for path in RULE_FILES:
        for rule in _rules(path):
            rule_id = rule["id"]
            assert rule_id.startswith("kit."), f"{path.name}:{rule_id} is not kit.*-namespaced"
            assert rule_id not in seen, f"{rule_id} is declared twice"
            seen.add(rule_id)


def test_every_rule_is_an_error_with_a_message() -> None:
    for path in RULE_FILES:
        for rule in _rules(path):
            assert rule["severity"] == "ERROR", f"{path.name}:{rule['id']}"
            assert str(rule["message"]).strip(), f"{path.name}:{rule['id']} has no message"


# --- paths: scoping, proved by a real scan (`--test` ignores `paths:`) ---


def test_runtime_tunable_literal_settings_exclusion_via_real_scan() -> None:
    fixture = FIXTURES / "src/config.py"
    findings = real_scan("security.yml", fixture)
    assert_scoping(findings, "kit.security.runtime-tunable-literal", fixture)


def test_http_exception_handlers_exclusion_via_real_scan() -> None:
    fixture = FIXTURES / "src/errors.py"
    findings = real_scan("errors.yml", fixture)
    assert_scoping(findings, "kit.errors.http-exception-outside-handlers", fixture)


def test_request_id_middleware_exclusion_via_real_scan() -> None:
    fixture = FIXTURES / "src/main.py"
    findings = real_scan("security.yml", fixture)
    assert_scoping(findings, "kit.security.request-id-outside-middleware", fixture)
    assert_scoping(findings, "kit.security.request-id-read-outside-middleware", fixture)


def test_deterministic_core_scope_via_real_scan() -> None:
    fixture = FIXTURES / "src/core/scoring.py"
    assert_scoping(
        real_scan("determinism.yml", fixture), "kit.determinism.no-environment-or-io", fixture
    )
    assert_scoping(
        real_scan("security.yml", fixture),
        "kit.security.environment-read-outside-settings",
        fixture,
    )


def test_application_layer_rules_scoped_to_src_via_real_scan() -> None:
    outside = FIXTURES / "outside_src/tooling.py"
    inside = FIXTURES / "src/repositories/scoped_violations.py"
    for config, rule_ids in [
        (
            "errors.yml",
            (
                "kit.errors.no-builtin-raise",
                "kit.errors.http-exception-outside-handlers",
                "kit.errors.envelope-outside-handlers",
            ),
        ),
        ("security.yml", ("kit.security.environment-read-outside-settings",)),
        ("logging.yml", ("kit.logging.no-secret-kwarg-in-log-call",)),
    ]:
        findings = real_scan(config, outside, inside)
        for rule_id in rule_ids:
            assert_scoping(findings, rule_id, outside, inside)


def test_configurable_sdk_import_scoped_outside_the_gateway_via_real_scan() -> None:
    inside = CONFIGURABLE_FIXTURES / "src/gateway/client.py"
    outside = CONFIGURABLE_FIXTURES / "src/orders/checkout.py"
    findings = real_scan(CONFIGURABLE_DIR / "gateway.yml", inside, outside)
    assert_scoping(findings, "kit.configurable.sdk-import-outside-gateway", inside, outside)


def test_nosemgrep_is_caught_by_a_real_scan() -> None:
    # `semgrep --test` self-suppresses a line carrying the semgrep suppression comment even
    # under `--disable-nosem`; a real scan with `--disable-nosem` reports it.
    fixture = FIXTURES / "suppressions.py"
    findings = real_scan("suppressions.yml", fixture, extra=("--disable-nosem",))
    reported = findings.get("kit.suppressions.python-comment-suppression", {}).get(
        str(fixture), set()
    )
    # Split so this file never spells the marker contiguously.
    marker = "#" + " nosemgrep"
    line = next(
        index
        for index, text in enumerate(fixture.read_text().splitlines(), start=1)
        if text.rstrip().endswith(marker)
    )
    assert line in reported
