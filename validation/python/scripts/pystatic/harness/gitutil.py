"""Git helpers shared by every step ("tool first"): git
already knows what is vendored, generated or ignored."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pystatic.harness.models import REPO_ROOT

_GIT = shutil.which("git") or "git"


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        [_GIT, *args], cwd=cwd or REPO_ROOT, capture_output=True, text=True, check=False
    )
    return proc.stdout


def merge_base(base_ref: str | None = None) -> str | None:
    if base_ref is None:
        upstream = _git("rev-parse", "--abbrev-ref", "@{u}").strip()
        if upstream:
            mb = _git("merge-base", upstream, "HEAD").strip()
            if mb:
                return mb
        base_ref = "origin/main"
    return _git("merge-base", base_ref, "HEAD").strip() or None


def changed_files(base_ref: str | None = None) -> set[str]:
    """Repo-relative changed-vs-merge-base paths plus untracked files, minus deletions."""
    base = merge_base(base_ref)
    changed: set[str] = set()
    if base:
        diff_out = _git("diff", "--name-only", base).splitlines()
        changed |= {ln.strip() for ln in diff_out if ln.strip()}
    untracked = _git("ls-files", "--others", "--exclude-standard").splitlines()
    changed |= {ln.strip() for ln in untracked if ln.strip()}
    return {f for f in changed if (REPO_ROOT / f).exists()}


def glob_tracked(pattern: str, cwd: Path | None = None) -> list[str]:
    """Every tracked/untracked-but-not-ignored file under `cwd` matching pathspec `pattern`."""
    base = cwd or REPO_ROOT
    out = _git("ls-files", "--cached", "--others", "--exclude-standard", "--", pattern, cwd=base)
    files = {ln.strip() for ln in out.splitlines() if ln.strip()}
    return sorted(f for f in files if (base / f).exists())
