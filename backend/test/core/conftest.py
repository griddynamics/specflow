"""
Conftest for test/core/ — restores real configure_logging for this directory.

test/api/conftest.py patches app.core.logging.configure_logging with a Mock at
module-import time to prevent filesystem errors when app.main is imported during
collection.  That patch is never restored, so it leaks into every test in the
session that accesses the module attribute.

Tests in test/core/ that exercise configure_logging directly need the real
implementation.  This fixture restores it via monkeypatch so it is automatically
reverted after each test without affecting other directories.
"""

import sys

import pytest

import app.core.logging as log_mod


@pytest.fixture(autouse=True)
def _restore_real_configure_logging(monkeypatch):
    api_conftest = sys.modules.get("test.api.conftest")
    if api_conftest is not None:
        real_fn = getattr(api_conftest, "original_configure", None)
        if real_fn is not None:
            monkeypatch.setattr(log_mod, "configure_logging", real_fn)
    yield
