"""Fixtures for semgrep/determinism.yml, named to match the
config's own stem so `semgrep --test` discovers it (`--test` ignores
`paths:`, so `paths.include` itself is not exercised here)."""

import os
import random
import secrets
import time
import uuid
from datetime import date, datetime

# --- kit.determinism.no-nondeterministic-source ---


def score_window(now: float) -> float:
    # ruleid: kit.determinism.no-nondeterministic-source
    started = time.time()
    return now - started


def stamp() -> datetime:
    # ruleid: kit.determinism.no-nondeterministic-source
    return datetime.utcnow()


def today_label() -> str:
    # ruleid: kit.determinism.no-nondeterministic-source
    return date.today().isoformat()


def sample_weight() -> float:
    # ruleid: kit.determinism.no-nondeterministic-source
    return random.random()


def token() -> str:
    # ruleid: kit.determinism.no-nondeterministic-source
    return secrets.token_hex(8)


def new_id() -> str:
    # ruleid: kit.determinism.no-nondeterministic-source
    return str(uuid.uuid4())


def score_from_inputs(now: float, offset: int) -> float:
    # ok: kit.determinism.no-nondeterministic-source
    return now - offset


# --- kit.determinism.no-environment-or-io ---


def read_cap(default: int) -> int:
    # ruleid: kit.determinism.no-environment-or-io
    return int(os.environ.get("MAX_DEPTH", default))


def load_weights(path: str) -> str:
    # ruleid: kit.determinism.no-environment-or-io
    with open(path) as fh:
        return fh.read()


def read_cap_via_getenv(default: str) -> str | None:
    # ruleid: kit.determinism.no-environment-or-io
    return os.getenv("MAX_DEPTH", default)


def rank_pure(candidates: list[float], weights: dict[str, float]) -> list[float]:
    # ok: kit.determinism.no-environment-or-io
    return sorted(candidates, key=lambda c: c * weights["primary"])


# --- kit.determinism.no-async-def ---

# The pattern anchors on the `async def` signature line itself (the first
# line of the match), not a line inside the body -- every marker below sits
# directly above the `async def`/`def` line it describes, never inside it.


# ruleid: kit.determinism.no-async-def
async def rank_async(candidates: list[float]) -> list[float]:
    return sorted(candidates)


# ok: kit.determinism.no-async-def
def rank_sync(candidates: list[float]) -> list[float]:
    # ok: kit.determinism.no-async-def
    def inner_helper(values: list[float]) -> list[float]:
        return sorted(values)

    return inner_helper(candidates)


# ruleid: kit.determinism.no-async-def
async def gather_scores(values: list[float]) -> list[float]:
    total = 0.0
    for value in values:
        total += value

    # nested async defs are flagged too
    # ruleid: kit.determinism.no-async-def
    async def inner_async(v: float) -> float:
        return v

    return [total, inner_async]


# --- kit.determinism.no-await-expression ---


# ruleid: kit.determinism.no-async-def
async def compute(value: float) -> float:
    # ok: kit.determinism.no-await-expression
    return value


# ruleid: kit.determinism.no-async-def
async def gather_with_await(values: list[float]) -> list[float]:
    total = 0.0
    for value in values:
        # ruleid: kit.determinism.no-await-expression
        total += await compute(value)
    return [total]
