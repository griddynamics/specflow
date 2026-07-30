"""Install a ``prepare-commit-msg`` git hook that stamps the current implementation-plan
phase onto each commit subject (``pNN_``).

The phase attribution used by P10Y must not depend on the agent's commit hygiene, so the
prefix is injected deterministically by git itself. The current phase number is read from
``git config specflow.phase``, which the codegen loop (``execute_all_phases``) sets before
each phase runs. Commits made outside that window (seed, janitor finalize, deploy) have no
(or a blank) marker and are left unprefixed — they fall into the ``unphased`` bucket or stay
excluded (``SKIP_*``).
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Managed hook body. ``--no-verify`` bypasses pre-commit and commit-msg hooks but NOT
# prepare-commit-msg, so this fires on every commit the agent makes.
PREPARE_COMMIT_MSG_HOOK = """\
#!/bin/sh
# SpecFlow: prefix the commit subject with the current implementation-plan phase (pNN_).
# Managed file — regenerated on every workspace setup. Do not edit by hand.
f="$1"
first=$(head -n 1 "$f")
case "$first" in
  SKIP_*|skip_*) exit 0 ;;   # never touch excluded seed/janitor commits
  p[0-9][0-9]_*) exit 0 ;;   # already prefixed (amend/rebase idempotency)
esac
phase=$(git config --get specflow.phase 2>/dev/null) || exit 0
case "$phase" in ''|*[!0-9]*) exit 0 ;; esac   # unset/non-numeric -> leave unphased
rest=$(tail -n +2 "$f")
printf 'p%02d_%s\\n%s' "$phase" "$first" "$rest" > "$f"
"""


def ensure_workspace_git_hooks(workspace_path: Path) -> bool:
    """Install the ``prepare-commit-msg`` phase-stamping hook into the workspace repo.

    Idempotent (overwrites the managed file). Safe to call from init/prep paths: I/O
    failures are logged and swallowed so they never propagate to abort git init or
    workspace preparation.

    Returns:
        True if the hook was written, False if unchanged/skipped or on error.
    """
    try:
        return _ensure_workspace_git_hooks(workspace_path)
    except OSError as exc:
        logger.warning(
            "Could not install prepare-commit-msg hook in %s (non-fatal): %s",
            workspace_path,
            exc,
        )
        return False


def _ensure_workspace_git_hooks(workspace_path: Path) -> bool:
    if not (workspace_path / ".git").is_dir():
        logger.warning(
            "No .git directory in %s — skipping prepare-commit-msg hook install",
            workspace_path,
        )
        return False

    hooks_dir = workspace_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "prepare-commit-msg"
    hook_path.write_text(PREPARE_COMMIT_MSG_HOOK, encoding="utf-8")
    hook_path.chmod(0o755)
    logger.info("Installed prepare-commit-msg phase hook in %s", hooks_dir)
    return True
