"""TEMPORARY worker diagnostics for the worker-parse freeze (2026-10-20).

DEBUG ONLY: revert the whole commit that added this module once the root cause
is known. Production showed a worker process that stopped heartbeating and was
then killed and restarted without a single log line. This module collects the
evidence that a frozen or killed process cannot log itself:

- ``worker.diag.boot``: the real CPU and memory limits of the container
  (cgroup ``cpu.max``/``memory.max``), the CPU count Python sees, OOM counters.
- ``worker.diag.previous_run_died``: a state file in ``/tmp`` (an ``emptyDir``
  that survives a container restart in the same pod) is rewritten every few
  seconds and removed on a clean stop. When the next process finds it, the
  previous one died; the line carries its last known state.
- ``worker.diag.resources``: memory, CPU use and throttling, event-loop lag and
  the running activity, every 15 s while an activity runs (120 s when idle).
- ``worker.diag.loop_stall``: the event loop did not tick for 20 s; one line
  per thread with its newest frames (works when the GIL is free).
- ``faulthandler``: a C-level timer dumps every thread's stack to stderr when
  the event loop does not re-arm it for 45 s (works even when the GIL is
  held), plus a dump on SIGTERM and on fatal signals (segfault, abort).
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import json
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from logger.logger import INFO, WARNING, get_logger, log_event
from temporal.containment import Contain

logger = get_logger("temporal.diagnostics")

#: The live process environment (the same mapping as ``os.environ``), read only for these debug-only keys.
_PROCESS_ENVIRON: Final[Mapping[str, str]] = os.environ
STATE_ENV: Final = "WORKER_DIAG_STATE"
STATE_FILE_NAME: Final = "specflow-worker-state.json"
CGROUP: Final = Path("/sys/fs/cgroup")
LOOP_TICK_SECONDS: Final = 5.0
STALL_DUMP_SECONDS: Final = 45.0
STALL_LOG_SECONDS: Final = 20.0
SAMPLE_SECONDS: Final = 5.0
LOG_BUSY_SECONDS: Final = 15.0
LOG_IDLE_SECONDS: Final = 120.0
STACK_FRAMES: Final = 12
#: ``key value`` / ``quota period`` lines carry exactly this many fields.
PAIR_FIELDS: Final = 2
#: The loop is reported late (``worker.diag.loop_late``) after this many milliseconds of lag.
LOOP_LATE_MS: Final = 2000
#: Seconds the monitor thread gets to end on ``stop()``.
STOP_JOIN_SECONDS: Final = 2

_lock = threading.Lock()
_activity: dict[str, Any] = {}
_loop: dict[str, float] = {"lag_ms": 0.0, "last_tick": time.monotonic()}


# -- what the activities report ------------------------------------------------
def set_activity(**fields: Any) -> None:
    """The activity that runs now (one at a time on the parse queue)."""

    now = time.monotonic()
    with _lock:
        _activity.clear()
        _activity.update(fields, started=now, stage_started=now)


def set_stage(stage: str) -> None:
    with _lock:
        if _activity:
            _activity.update(stage=stage, stage_started=time.monotonic())


def clear_activity() -> None:
    with _lock:
        _activity.clear()


def activity_view() -> dict[str, Any]:
    """The running activity with elapsed seconds instead of monotonic stamps."""

    now = time.monotonic()
    with _lock:
        view = dict(_activity)
    if not view:
        return {}
    view["elapsed_s"] = round(now - view.pop("started"), 1)
    view["stage_elapsed_s"] = round(now - view.pop("stage_started"), 1)
    return view


def loop_lag_ms() -> int:
    return int(_loop["lag_ms"])


def rss_mb() -> float | None:
    return _proc_status_mb("VmRSS")


# -- readers (best effort, never raise) ------------------------------------------
def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _int(path: Path) -> int | None:
    text = _read(path)
    if text is None or not text.lstrip("-").isdigit():
        return None
    return int(text)


def _pairs(path: Path) -> dict[str, int]:
    """``key value`` lines (``cpu.stat``, ``memory.events``) as ints."""

    result: dict[str, int] = {}
    for line in (_read(path) or "").splitlines():
        parts = line.split()
        if len(parts) == PAIR_FIELDS and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


def _proc_status_mb(key: str) -> float | None:
    for line in (_read(Path("/proc/self/status")) or "").splitlines():
        if line.startswith(f"{key}:"):
            parts = line.split()
            if len(parts) >= PAIR_FIELDS and parts[1].isdigit():
                return round(int(parts[1]) / 1024, 1)
    return None


def _mb(value: int | None) -> float | None:
    return None if value is None else round(value / 1024 / 1024, 1)


def limits(root: Path = CGROUP) -> dict[str, Any]:
    """Container limits as the kernel enforces them (cgroup v2, v1 fallback)."""

    affinity = None
    if sys.platform == "linux":
        with contextlib.suppress(OSError):
            affinity = len(os.sched_getaffinity(0))
    memory_max = _read(root / "memory.max")
    if memory_max is None:  # cgroup v1
        memory_max = _read(root / "memory" / "memory.limit_in_bytes")
    cpu_max = _read(root / "cpu.max")
    if cpu_max is None:  # cgroup v1: "<quota> <period>"
        quota = _read(root / "cpu" / "cpu.cfs_quota_us")
        period = _read(root / "cpu" / "cpu.cfs_period_us")
        cpu_max = None if quota is None else f"{quota} {period}"
    events = _pairs(root / "memory.events")
    return {
        "cpu_max": cpu_max,
        "cpu_limit_cores": _cores(cpu_max),
        "memory_max": memory_max,
        "memory_max_mb": _mb(int(memory_max)) if memory_max is not None and memory_max.isdigit() else None,
        "os_cpu_count": os.cpu_count(),
        "sched_affinity": affinity,
        "omp_num_threads": _PROCESS_ENVIRON.get("OMP_NUM_THREADS"),
        "oom_events": events.get("oom"),
        "oom_kill_events": events.get("oom_kill"),
        "pid": os.getpid(),
    }


def _cores(cpu_max: str | None) -> float | None:
    parts = (cpu_max or "").split()
    if len(parts) != PAIR_FIELDS or not parts[0].lstrip("-").isdigit() or not parts[1].isdigit():
        return None  # "max 100000" (no limit) or unreadable
    quota, period = int(parts[0]), int(parts[1])
    return None if quota <= 0 or period == 0 else round(quota / period, 2)


def snapshot(root: Path = CGROUP) -> dict[str, Any]:
    """One sample of the process and its container; cumulative counters raw."""

    cpu = _pairs(root / "cpu.stat")
    events = _pairs(root / "memory.events")
    return {
        "wall": round(time.time(), 3),
        "rss_mb": _proc_status_mb("VmRSS"),
        "rss_peak_mb": _proc_status_mb("VmHWM"),
        "threads": threading.active_count(),
        "cgroup_memory_mb": _mb(_int(root / "memory.current")),
        "cgroup_memory_peak_mb": _mb(_int(root / "memory.peak")),
        "oom_events": events.get("oom"),
        "oom_kill_events": events.get("oom_kill"),
        "cpu_usage_usec": cpu.get("usage_usec"),
        "cpu_nr_periods": cpu.get("nr_periods"),
        "cpu_nr_throttled": cpu.get("nr_throttled"),
        "cpu_throttled_usec": cpu.get("throttled_usec"),
        "loop_lag_ms": loop_lag_ms(),
        "loop_tick_age_s": round(time.monotonic() - _loop["last_tick"], 1),
        "activity": activity_view(),
    }


def with_rates(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Add CPU cores used and the throttled share over the last interval."""

    result = dict(current)
    if previous is None:
        return result
    seconds = current["wall"] - previous["wall"]

    def delta(key: str) -> int | None:
        a, b = previous.get(key), current.get(key)
        return None if a is None or b is None else b - a

    usage, periods, throttled = (
        delta("cpu_usage_usec"),
        delta("cpu_nr_periods"),
        delta("cpu_nr_throttled"),
    )
    if usage is not None and seconds > 0:
        result["cpu_cores_used"] = round(usage / 1_000_000 / seconds, 2)
    if periods and throttled is not None:
        result["cpu_throttled_pct"] = round(100 * throttled / periods, 1)
    return result


# -- the state file that outlives a killed process --------------------------------
def state_path() -> Path:
    """``WORKER_DIAG_STATE``, else the state file in the temp folder, looked up at start (not at import).

    A read-only root filesystem without a writable ``/tmp`` (worker-agents on Kubernetes) has no temp
    folder: the worker must still start, so the name alone is returned, its writes fail and
    ``write_state`` reports ``worker.diag.state_unwritable`` once.
    """

    configured = _PROCESS_ENVIRON.get(STATE_ENV)
    if configured:
        return Path(configured)
    try:
        return Path(tempfile.gettempdir()) / STATE_FILE_NAME
    except FileNotFoundError:
        return Path(STATE_FILE_NAME)


def write_state(path: Path, sample: dict[str, Any]) -> bool:
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(sample, default=str))
        tmp.replace(path)
    except OSError:
        return False
    return True


def report_previous_run(path: Path) -> dict[str, Any] | None:
    """Log the last state of a process that did not stop cleanly; then forget it."""

    text = _read(path)
    if text is None:
        return None
    with contextlib.suppress(OSError):
        path.unlink()
    try:
        state = json.loads(text)
    except ValueError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    fields = {f"last_{key}": value for key, value in state.items() if key != "wall"}
    wall = state.get("wall")
    age = round(time.time() - wall, 1) if isinstance(wall, (int, float)) else None
    log_event(logger, WARNING, "worker.diag.previous_run_died", seconds_ago=age, **fields)
    return state


# -- stacks ---------------------------------------------------------------------
def thread_stacks() -> list[dict[str, Any]]:
    """Each thread's newest frames as compact ``file:line func`` text."""

    names = {thread.ident: thread.name for thread in threading.enumerate()}
    stacks = []
    for ident, frame in sys._current_frames().items():
        frames = traceback.extract_stack(frame)[-STACK_FRAMES:]
        text = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames))
        stacks.append({"thread_name": names.get(ident, str(ident)), "frames": text})
    return stacks


# -- runtime --------------------------------------------------------------------
class Diagnostics:
    """Started once by the worker runner; ``stop()`` marks a clean exit."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._monitor, name="worker-diag", daemon=True)
        self._loop_task: asyncio.Task[None] | None = None
        self._stderr = sys.__stderr__
        self._sigterm = False

    def start(self) -> None:
        report_previous_run(self._path)
        log_event(logger, INFO, "worker.diag.boot", **limits())
        self._install_faulthandler()
        self._loop_task = asyncio.get_running_loop().create_task(self._watch_loop())
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
        with contextlib.suppress(Exception):
            faulthandler.cancel_dump_traceback_later()
        if self._sigterm:
            with contextlib.suppress(Exception):
                faulthandler.unregister(signal.SIGTERM)
        self._thread.join(timeout=STOP_JOIN_SECONDS)
        with contextlib.suppress(OSError):
            self._path.unlink()

    def _install_faulthandler(self) -> None:
        """Fatal signals and SIGTERM dump all stacks; call after the loop's handlers."""

        if self._stderr is None:
            return
        with contextlib.suppress(Exception):
            if not faulthandler.is_enabled():
                faulthandler.enable(file=self._stderr, all_threads=True)
        with contextlib.suppress(Exception):
            faulthandler.register(signal.SIGTERM, file=self._stderr, all_threads=True, chain=True)
            self._sigterm = True

    async def _watch_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if self._stderr is not None:
                with contextlib.suppress(Exception):
                    faulthandler.dump_traceback_later(STALL_DUMP_SECONDS, repeat=True, file=self._stderr)
            started = loop.time()
            await asyncio.sleep(LOOP_TICK_SECONDS)
            lag = (loop.time() - started - LOOP_TICK_SECONDS) * 1000
            _loop["lag_ms"] = max(lag, 0.0)
            _loop["last_tick"] = time.monotonic()
            if lag > LOOP_LATE_MS:
                log_event(
                    logger,
                    WARNING,
                    "worker.diag.loop_late",
                    lag_ms=int(lag),
                    activity=activity_view(),
                )

    def _monitor(self) -> None:
        previous: dict[str, Any] | None = None
        last_log = 0.0
        last_stall_log = 0.0
        unwritable_logged = False
        while not self._stop.wait(SAMPLE_SECONDS):
            with Contain() as contained:
                sample = with_rates(snapshot(), previous)
                previous = sample
                if not write_state(self._path, sample) and not unwritable_logged:
                    unwritable_logged = True
                    log_event(logger, WARNING, "worker.diag.state_unwritable", path=str(self._path))
                now = time.monotonic()
                every = LOG_BUSY_SECONDS if sample["activity"] else LOG_IDLE_SECONDS
                if now - last_log >= every:
                    last_log = now
                    log_event(logger, INFO, "worker.diag.resources", **sample)
                tick_age = now - _loop["last_tick"]
                if tick_age >= STALL_LOG_SECONDS and now - last_stall_log >= STALL_LOG_SECONDS:
                    last_stall_log = now
                    for stack in thread_stacks():
                        log_event(
                            logger,
                            WARNING,
                            "worker.diag.loop_stall",
                            loop_tick_age_s=round(tick_age, 1),
                            **stack,
                        )
            if contained.error is not None:
                log_event(logger, WARNING, "worker.diag.monitor_error", error_type=contained.error_type)


def start() -> Diagnostics | None:
    """Start the diagnostics inside the worker's event loop; never raises."""

    with Contain() as contained:
        diagnostics = Diagnostics(state_path())
        diagnostics.start()
    if contained.error is not None:
        log_event(logger, WARNING, "worker.diag.start_failed", error_type=contained.error_type)
        return None
    return diagnostics
