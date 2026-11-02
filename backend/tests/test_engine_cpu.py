"""``engines.cpu``: the CPU affinity capped by the cgroup CPU quota (docker --cpus, Kubernetes, Cloud Run).

The cgroup files are written under ``tmp_path`` (the root is injectable); the affinity is pinned per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from file_parsers.engines import cpu
from file_parsers.engines.cpu import available_cpus, quota_cpus


@pytest.fixture
def four_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cpu.os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3}, raising=False)


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="ascii")


@pytest.mark.usefixtures("four_cpus")
@pytest.mark.parametrize(
    ("files", "quota", "available"),
    [
        ({"cpu.max": "200000 100000"}, 2, 2),  # v2: docker run --cpus 2
        ({"cpu.max": "150000 100000"}, 2, 2),  # v2: 1.5 CPUs round up
        ({"cpu.max": "50000 100000"}, 1, 1),  # v2: half a CPU is still one thread
        ({"cpu.max": "800000 100000"}, 8, 4),  # v2: a quota above the affinity does not raise it
        ({"cpu.max": "max 100000"}, None, 4),  # v2: no quota
        ({"cpu/cpu.cfs_quota_us": "200000", "cpu/cpu.cfs_period_us": "100000"}, 2, 2),  # v1
        ({"cpu,cpuacct/cpu.cfs_quota_us": "300000", "cpu,cpuacct/cpu.cfs_period_us": "100000"}, 3, 3),  # v1 alt
        ({"cpu/cpu.cfs_quota_us": "-1", "cpu/cpu.cfs_period_us": "100000"}, None, 4),  # v1: no quota
        ({}, None, 4),  # no cgroup files
        ({"cpu.max": "garbage"}, None, 4),
        ({"cpu.max": "abc 100000"}, None, 4),
        ({"cpu/cpu.cfs_quota_us": "200000"}, None, 4),  # v1 without the period
        ({"cpu.max": "100000 0"}, None, 4),
    ],
)
def test_the_quota_caps_the_affinity(files: dict[str, str], quota: int | None, available: int, tmp_path: Path):
    for name, text in files.items():
        write(tmp_path, name, text)
    assert quota_cpus(tmp_path) == quota
    assert available_cpus(tmp_path) == available


@pytest.mark.usefixtures("four_cpus")
def test_an_unreadable_quota_file_falls_back_to_the_affinity(tmp_path: Path):
    (tmp_path / "cpu.max").mkdir()  # reading a directory raises OSError
    assert quota_cpus(tmp_path) is None
    assert available_cpus(tmp_path) == 4


def test_without_affinity_the_cpu_count_is_used_and_never_below_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delattr(cpu.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(cpu.os, "cpu_count", lambda: None)
    assert available_cpus(tmp_path) == 1
    monkeypatch.setattr(cpu.os, "cpu_count", lambda: 6)
    write(tmp_path, "cpu.max", "200000 100000")
    assert available_cpus(tmp_path) == 2


def test_the_default_root_is_the_cgroup_mount():
    assert Path("/sys/fs/cgroup") == cpu.CGROUP_ROOT
    assert available_cpus() >= 1
