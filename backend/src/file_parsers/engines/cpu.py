"""The CPUs this process may really use: its CPU affinity, capped by the cgroup CPU quota.

``os.sched_getaffinity`` lists every host CPU in a container started with ``docker run --cpus 2``, a Kubernetes
CPU limit or a Cloud Run CPU setting: those cap CPU time (a CFS quota), not the CPUs. Running more Docling threads
than the quota allows makes them wait for CPU time (slower, and more memory held while they wait). The quota is
read from cgroup v2 ``cpu.max`` (``"<quota> <period>"``, ``"max <period>"`` = none) or cgroup v1
``cpu.cfs_quota_us``/``cpu.cfs_period_us`` (``-1`` = none); a fractional quota rounds up (1.5 CPUs -> 2).
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

CGROUP_ROOT = Path("/sys/fs/cgroup")
_V1_DIRS = ("cpu", "cpu,cpuacct")
_V2_FIELDS = 2  # "<quota> <period>"


def affinity_cpus() -> int:
    """The CPUs the scheduler lets this process run on (``os.cpu_count`` where affinity is unknown)."""

    scheduler: Any = os
    try:
        return max(1, len(scheduler.sched_getaffinity(0)))
    except AttributeError:  # not Linux
        return max(1, os.cpu_count() or 1)


def quota_cpus(root: Path = CGROUP_ROOT) -> int | None:
    """The cgroup CPU quota in whole CPUs (rounded up), or ``None`` when there is none or it cannot be read."""

    v2 = _read(root / "cpu.max")
    if v2 is not None:
        parts = v2.split()
        return _cpus(parts[0], parts[1]) if len(parts) == _V2_FIELDS else None
    for directory in _V1_DIRS:
        quota = _read(root / directory / "cpu.cfs_quota_us")
        period = _read(root / directory / "cpu.cfs_period_us")
        if quota is not None and period is not None:
            return _cpus(quota, period)
    return None


def available_cpus(root: Path = CGROUP_ROOT) -> int:
    """``min(affinity, quota)``, at least 1; the affinity alone when there is no readable quota."""

    affinity = affinity_cpus()
    quota = quota_cpus(root)
    return max(1, affinity if quota is None else min(affinity, quota))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _cpus(quota: str, period: str) -> int | None:
    try:
        quota_us, period_us = int(quota), int(period)
    except ValueError:  # "max" (v2: no quota) or garbage
        return None
    if quota_us <= 0 or period_us <= 0:  # v1: -1 = no quota
        return None
    return max(1, math.ceil(quota_us / period_us))
