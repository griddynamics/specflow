"""Shared fixtures for the MCP server test suite."""

import os
import time
from contextlib import contextmanager
from typing import Iterator

import pytest


@contextmanager
def _pinned_timezone(name: str) -> Iterator[None]:
    """Run the block with the process timezone pinned, so local-time assertions are stable."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.fixture
def local_timezone():
    """Context-manager factory pinning the local timezone (see ``_pinned_timezone``)."""
    return _pinned_timezone
