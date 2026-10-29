"""Fixtures for traceability.yml."""

import pytest

# --- kit.configurable.test-must-carry-marker ---


# ok: kit.configurable.test-must-carry-marker
@pytest.mark.requirement("REQ-01")
def test_ok():
    pass


# ok: kit.configurable.test-must-carry-marker
@pytest.mark.asyncio
@pytest.mark.requirement("REQ-02")
async def test_async_ok():
    pass


# ruleid: kit.configurable.test-must-carry-marker
def test_missing_marker():
    pass


# ruleid: kit.configurable.test-must-carry-marker
async def test_async_missing_marker():
    pass


# ok: kit.configurable.test-must-carry-marker
def helper_function():
    pass
