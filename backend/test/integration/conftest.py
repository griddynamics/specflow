"""
Integration test fixtures.

Imports shared db fixture from jobs.conftest since JobFakeDB has both
state machine interface and query interface needed for integration tests.
"""
import sys
from pathlib import Path

# Add parent test directory to path to allow imports
test_dir = Path(__file__).parent.parent
sys.path.insert(0, str(test_dir))

# Import db fixture from jobs conftest
from jobs.conftest import db  # noqa: E402, F401

# Re-export for pytest discovery
__all__ = ["db"]
