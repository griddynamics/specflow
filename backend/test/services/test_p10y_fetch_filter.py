"""
Tests for P10Y commit stats filtering against local-git SHA allowlist.
"""

import logging
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.p10y.p10y_lib import fetch_and_filter_commit_stats


def _row(sha: str) -> dict:
    return {
        "sha": sha,
        "fp_delta_total": 1.0,
        "commit_quality_score": 0.5,
        "churn_rate": 0.0,
        "refactor": 0.0,
        "rework": 0.0,
        "new_work": 1.0,
        "removed_work": 0.0,
        "quality_score": 0.5,
        "effective_output": 1.0,
        "total_output": 1.0,
        "technologies": {"supported": ["python"]},
    }


@pytest.mark.asyncio
async def test_fetch_and_filter_only_exact_local_shas() -> None:
    """Stale P10Y rows (same 7-char prefix as current) must not be included."""
    current = "aaaaaaaabbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    stale = "aaaaaaaacccccccccccccccccccccccccccccccc"  # same first 7 as current

    client = Mock()
    client.get_commit_stats = AsyncMock(
        return_value=Mock(
            data=[
                _row(current),
                _row(stale),
            ]
        )
    )

    logger = logging.getLogger("test_p10y_filter")
    out = await fetch_and_filter_commit_stats(
        client=client,
        repository_id=1,
        organisation_id=1,
        allowed_commit_shas=[current],
        workspace_name="ws-1",
        logger=logger,
    )

    assert len(out) == 1
    assert out[0]["sha"] == current


@pytest.mark.asyncio
async def test_fetch_and_filter_case_insensitive_and_dedupes() -> None:
    client = Mock()
    full = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    client.get_commit_stats = AsyncMock(
        return_value=Mock(
            data=[
                _row(full.upper()),
                _row(full),
            ]
        )
    )

    logger = logging.getLogger("test_p10y_filter2")
    out = await fetch_and_filter_commit_stats(
        client=client,
        repository_id=1,
        organisation_id=1,
        allowed_commit_shas=[full],
        workspace_name="ws-1",
        logger=logger,
    )

    assert len(out) == 1
