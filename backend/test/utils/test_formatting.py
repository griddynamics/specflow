"""Tests for app.utils.formatting."""

import pytest

from app.schemas.model_token_usage import ModelTokenUsage
from app.utils.formatting import format_token_count, format_token_usage_lines


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0"),
        (842, "842"),
        (999, "999"),
        (1000, "1k"),
        (125_000, "125k"),
        (999_999, "999k"),
        (1_000_000, "1m"),
        (1_200_000, "1.2m"),
        (9_900_000, "9.9m"),
        (10_000_000, "10m"),
        (-5, "0"),
    ],
)
def test_format_token_count(n: int, expected: str) -> None:
    assert format_token_count(n) == expected


def test_format_token_usage_lines_single_tokens_header() -> None:
    """Token lines use one 'Tokens:' block and indented buckets (no repeated 'cumulative')."""
    u = ModelTokenUsage(
        model_name="m",
        num_turns=3,
        input_tokens=1200,
        output_tokens=800,
        cache_write_tokens=0,
        cache_read_tokens=100,
    )
    lines = format_token_usage_lines(u)
    text = "\n".join(lines)
    assert "Agent turns: 3" in text
    assert text.count("Tokens (cumulative):") == 1
    assert "  input:" in text
    assert "  total:" in text
