"""
Regression: lifecycle ordering checks must not rely on assert.

Under ``python -O``, assert statements are stripped. ``GenerationLifecyclePolicy``
must still enforce ordering via ``_check_lifecycle_policy`` (ValueError) at class
definition time, so importing ``app.core.ttl_config`` under -O must succeed.
"""

import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_ttl_config_import_under_python_o() -> None:
    """Importing ttl_config with PYTHONOPTIMIZE semantics (-O) must load the class body."""
    code = (
        "from app.core.ttl_config import GenerationLifecyclePolicy as P; "
        "ok = ("
        "P.SESSION_ANALYSIS_MINUTES < P.SESSION_PLANNING_MINUTES < P.SESSION_GENERATION_MINUTES "
        "and P.SESSION_GENERATION_MINUTES > P.STUCK_RUNNING_MINUTES "
        "and (P.AGENT_PHASE_TIMEOUT_SECONDS // 60) < P.STUCK_RUNNING_MINUTES "
        "and (P.STUCK_CLEANING_HOURS * 60) < P.STUCK_RUNNING_MINUTES"
        "); "
        "raise SystemExit(0 if ok else 1)"
    )
    proc = subprocess.run(
        [sys.executable, "-O", "-c", code],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
