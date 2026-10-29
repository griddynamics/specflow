"""TEMPORARY tests of ``temporal.diagnostics`` (worker-parse freeze investigation).

Revert together with the module.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from temporal import diagnostics


def _cgroup(root: Path, **files: str) -> Path:
    for name, text in files.items():
        path = root / name.replace("__", "/").replace("_dot_", ".")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_limits_read_cgroup_v2(tmp_path: Path) -> None:
    root = _cgroup(
        tmp_path,
        cpu_dot_max="50000 100000\n",
        memory_dot_max="2147483648\n",
        memory_dot_events="low 0\nhigh 0\nmax 3\noom 2\noom_kill 1\n",
    )

    limits = diagnostics.limits(root)

    assert limits["cpu_max"] == "50000 100000"
    assert limits["cpu_limit_cores"] == 0.5
    assert limits["memory_max_mb"] == 2048.0
    assert limits["oom_events"] == 2
    assert limits["oom_kill_events"] == 1


def test_limits_without_cgroup_files_never_raise(tmp_path: Path) -> None:
    limits = diagnostics.limits(tmp_path)

    assert limits["cpu_max"] is None
    assert limits["cpu_limit_cores"] is None
    assert limits["memory_max_mb"] is None


def test_no_cpu_limit_is_reported_as_none() -> None:
    assert diagnostics._cores("max 100000") is None


def test_rates_give_cores_used_and_throttled_share(tmp_path: Path) -> None:
    previous = {"wall": 100.0, "cpu_usage_usec": 0, "cpu_nr_periods": 100, "cpu_nr_throttled": 10}
    current = {"wall": 110.0, "cpu_usage_usec": 5_000_000, "cpu_nr_periods": 200, "cpu_nr_throttled": 90}

    result = diagnostics.with_rates(current, previous)

    assert result["cpu_cores_used"] == 0.5
    assert result["cpu_throttled_pct"] == 80.0


def test_snapshot_carries_the_running_activity(tmp_path: Path) -> None:
    diagnostics.set_activity(activity="parse_document", document_id="d1", attempt=2, stage="load")
    diagnostics.set_stage("parse")
    try:
        sample = diagnostics.snapshot(tmp_path)
    finally:
        diagnostics.clear_activity()

    assert sample["activity"]["document_id"] == "d1"
    assert sample["activity"]["stage"] == "parse"
    assert sample["activity"]["attempt"] == 2
    assert "elapsed_s" in sample["activity"]
    json.dumps(sample)  # the state file must be JSON
    assert diagnostics.snapshot(tmp_path)["activity"] == {}


def test_a_left_over_state_file_is_reported_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "state.json"
    diagnostics.write_state(path, {"wall": 1.0, "rss_mb": 1900.0, "activity": {"stage": "parse"}})

    state = diagnostics.report_previous_run(path)

    assert state is not None and state["rss_mb"] == 1900.0
    assert not path.exists()
    record = next(r for r in caplog.records if r.getMessage() == "worker.diag.previous_run_died")
    assert record.last_rss_mb == 1900.0
    assert record.last_activity == {"stage": "parse"}
    assert diagnostics.report_previous_run(path) is None


def test_a_broken_state_file_is_still_reported(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")

    assert diagnostics.report_previous_run(path) == {}
    assert any(r.getMessage() == "worker.diag.previous_run_died" for r in caplog.records)


def test_thread_stacks_name_each_thread() -> None:
    stacks = diagnostics.thread_stacks()

    assert any(stack["thread_name"] == "MainThread" for stack in stacks)
    assert all(stack["frames"] for stack in stacks)


async def test_start_writes_state_and_a_clean_stop_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(diagnostics, "SAMPLE_SECONDS", 0.02)
    path = tmp_path / "state.json"
    monkeypatch.setenv(diagnostics.STATE_ENV, str(path))

    diag = diagnostics.start()
    assert diag is not None
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while not path.exists():
            assert asyncio.get_running_loop().time() < deadline, "no state file"
            await asyncio.sleep(0)
        assert "rss_mb" in json.loads(path.read_text())
    finally:
        diag.stop()

    assert not path.exists()
    assert any(r.getMessage() == "worker.diag.boot" for r in caplog.records)


def test_the_state_path_is_looked_up_at_start_not_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_temp_folder() -> str:
        raise FileNotFoundError("No usable temporary directory found")

    monkeypatch.setattr(diagnostics, "_PROCESS_ENVIRON", {})
    monkeypatch.setattr(diagnostics.tempfile, "gettempdir", no_temp_folder)

    assert diagnostics.state_path() == Path(diagnostics.STATE_FILE_NAME)


def test_the_state_path_uses_the_temp_folder_or_the_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(diagnostics, "_PROCESS_ENVIRON", {})
    assert diagnostics.state_path() == tmp_path / diagnostics.STATE_FILE_NAME

    monkeypatch.setattr(diagnostics, "_PROCESS_ENVIRON", {diagnostics.STATE_ENV: str(tmp_path / "s.json")})
    assert diagnostics.state_path() == tmp_path / "s.json"
