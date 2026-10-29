"""End to end: every check passes on the example service and fails on a broken config.

Each run is a fresh `python -m archkit` process, so the example's modules never leak into
this session. The configuration is copied to a temporary folder with its project root
pointing back at the example, and the OpenAPI snapshot is written there, so a self-test
run writes nothing inside the kit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "shop"


def _config(tmp_path: Path, replacements: dict[str, str]) -> Path:
    text = (EXAMPLE / "archkit.toml").read_text(encoding="utf-8")
    replacements = {
        'project_root = "."': f'project_root = "{EXAMPLE.as_posix()}"',
        'path = "contracts/openapi.json"': f'path = "{(tmp_path / "openapi.json").as_posix()}"',
        **replacements,
    }
    for old, new in replacements.items():
        assert old in text, f"the example configuration no longer contains {old!r}"
        text = text.replace(old, new)
    path = tmp_path / "archkit.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _archkit(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "archkit", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_every_check_passes_on_the_example_with_nothing_skipped(tmp_path: Path) -> None:
    config = _config(tmp_path, {})
    exported = _archkit("export-openapi", "--config", str(config))
    assert exported.returncode == 0, exported.stderr
    result = _archkit("run", "--config", str(config), "-q")
    summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in summary, summary
    assert "skipped" not in summary, result.stdout


def test_broken_configurations_fail_the_matching_checks(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {
            '{ code = "CONFLICT", http = 409 }': '{ code = "CONFLICT", http = 410 }',
            'header = "X-Request-Id"': 'header = "X-Trace-Id"',
            'create_operation_prefix = "create_"': 'create_operation_prefix = "read_"',
        },
    )
    result = _archkit(
        "run",
        "--config",
        str(config),
        "-q",
        "-k",
        "documented_code or 2xx_response_carries or return_201",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    for failed in (
        "test_every_handled_error_answers_a_documented_code",
        "test_a_2xx_response_carries_the_request_id",
        "test_create_operations_return_201",
    ):
        assert f"{failed} " in result.stdout or f"::{failed}" in result.stdout, result.stdout


def test_a_retired_section_or_a_missing_check_is_a_configuration_error(tmp_path: Path) -> None:
    config = _config(tmp_path, {"[http]": '[csrf]\nheader = "X-Requested-With"\n\n[http]'})
    result = _archkit("run", "--config", str(config), "-q")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "unknown section(s) ['csrf']" in result.stderr, result.stderr

    config = _config(tmp_path, {"[request_id]": "[request_id_retired]"})
    result = _archkit("run", "--config", str(config), "-q")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "unknown section(s) ['request_id_retired']" in result.stderr, result.stderr

    config = _config(tmp_path, {'ok_path = "/api/v1/health"': ""})
    result = _archkit("run", "--config", str(config), "-q")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "[request_id] ok_path" in result.stderr, result.stderr
