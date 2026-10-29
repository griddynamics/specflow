"""`pystatic`: project-agnostic static validation scripts for Python repositories.

Every check runs standalone as `python -m pystatic.scripts.<name> --root DIR` and through
the one runner `python -m pystatic.run --root DIR`. Kept import-free so the style checks
(`pystatic.style`) run in an environment without the harness dependencies."""

from __future__ import annotations
