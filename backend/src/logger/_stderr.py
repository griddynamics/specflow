"""The central logger's only self-diagnostic channel.

Nothing in `src/logger/` ever logs its own internal problems (config
errors, a handler that failed to emit, a dropped record) through the
central logging pipeline itself — that would risk recursion (a failure to
log spawning more "failed to log" records) and, worse, would make a broken
pipeline silently swallow the very warning that explains it. Instead every
such condition is written directly to `sys.stderr`, deduplicated so a
repeating failure cannot flood the terminal.

Rule: no self-logging. Fallback chain: stdout fails -> stderr;
stderr fails -> drop silently. Nothing below this
module exists to fall back on.
"""

from __future__ import annotations

import sys
import threading
import time

# Re-entrant (not a plain `threading.Lock`): `warn_once`/`warn_rate_limited`
# hold this while `_write()` calls `sys.stderr.write`. If that write itself
# somehow re-enters logging (e.g. a host app that redirects `sys.stderr`
# through a logging handler) and hits this module again on the SAME thread,
# a plain `Lock` would deadlock trying to re-acquire itself. An `RLock`
# lets the same thread back in without blocking -- but see `_write()`'s own
# thread-local guard below for the second, more direct layer of defense:
# this lock being reentrant only stops the *deadlock*, not the recursion
# itself.
_LOCK = threading.RLock()

# `warn_once` dedup state: keys that have already fired, for the life of
# the process (or until `_reset()`, which is test-only).
_seen_once: set[str] = set()

# `warn_rate_limited` state: the monotonic timestamp each key last fired.
_last_emitted: dict[str, float] = {}

# Per-thread re-entrancy guard for `_write()`. Belt-and-braces alongside the
# `RLock` above: even reentrant, a naive `_write()` would happily recurse
# forever if the write itself keeps re-triggering a call back into this
# module (e.g. stderr routed through logging, which hits a full queue,
# which warns again, ...). Tracking "am I already inside a write on this
# thread" lets a genuine re-entrant call short-circuit to a silent no-op
# instead of recursing into the write logic at all -- closer to
# crash_safety.md's "no self-logging/recursion" principle than merely
# making the lock reentrant would be.
_write_in_progress = threading.local()


def _write(message: str) -> bool:
    """Write one `[central-logger] ...` line to stderr; never raise.

    `sys.stderr` is looked up here, at call time, rather than cached at
    import time -- pytest's `capsys` (and similar stream-swapping tools)
    replace `sys.stderr` after this module is imported, so caching the
    stream object would silently write to a stale, unobserved stream.

    Re-entrancy guarded (see `_write_in_progress` above): a nested call on
    the same thread -- this thread's own stderr write somehow looping back
    into `warn_once`/`warn_rate_limited` -- returns `False` immediately
    rather than attempting (and potentially recursing through) a second
    write.
    """

    if getattr(_write_in_progress, "active", False):
        return False

    _write_in_progress.active = True
    try:
        sys.stderr.write(f"[central-logger] {message}\n")
    except Exception:
        # Total: a closed/broken stderr pipe drops the message silently
        # rather than propagating into application code.
        return False
    finally:
        _write_in_progress.active = False
    return True


def warn_once(key: str, message: str) -> bool:
    """Write `message` at most once per `key`, for the process lifetime.

    Returns `True` if this call actually wrote the line, `False` if it was
    suppressed (already fired for `key`) or if the write itself failed.
    """

    with _LOCK:
        if key in _seen_once:
            return False
        _seen_once.add(key)
        return _write(message)


def warn_rate_limited(key: str, message: str, *, interval: float = 60.0) -> bool:
    """Write `message` for `key` at most once per `interval` seconds.

    Returns `True` if this call actually wrote the line, `False` if it was
    rate-limited or if the write itself failed.
    """

    now = time.monotonic()
    with _LOCK:
        last = _last_emitted.get(key)
        if last is not None and (now - last) < interval:
            return False
        _last_emitted[key] = now
        return _write(message)


def reset_state() -> None:
    """Test-only: clear all dedup/rate-limit state."""

    with _LOCK:
        _seen_once.clear()
        _last_emitted.clear()


def _reset() -> None:
    """Test-only alias kept for the harness: same as ``reset_state``."""

    reset_state()
