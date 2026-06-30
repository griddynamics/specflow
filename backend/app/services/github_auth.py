"""
Resolve effective GitHub HTTPS credentials for an generation or API key document.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from app.core.github_platform_secrets import GithubPlatformSecrets, get_github_platform_secrets
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.database.interface import IDatabase
from app.state.db_adapter import COL_GENERATION_SESSIONS


@dataclass(frozen=True)
class GithubAuthContext:
    """Effective git user + PAT for https://user:token@host/... (never log `token`)."""

    git_user_name: str
    token: str


class GithubAuthResolutionError(Exception):
    """Raised when Git credentials cannot be resolved for the given context."""


def resolve_github_auth_for_api_key_document(
    api_key_doc: Dict[str, Any],
    secrets: GithubPlatformSecrets,
) -> GithubAuthContext:
    """
    Resolution order (plan):
    1. Encrypted per-key token in Firestore → decrypt.
    2. Else if workspace_pool == default → platform default PAT + default git user.
    3. Else dedicated pool → fail (customer must upload PAT).
    """
    pool = api_key_doc.get("workspace_pool") or DEFAULT_WORKSPACE_POOL
    ciphertext = api_key_doc.get("github_token_ciphertext")
    per_key_user = api_key_doc.get("git_user_name")

    if ciphertext:
        try:
            token = secrets.decrypt_token(str(ciphertext))
        except ValueError as e:
            raise GithubAuthResolutionError("Stored GitHub token could not be decrypted") from e
        user = (per_key_user or secrets.git_user_name_default or "x-access-token").strip()
        return GithubAuthContext(git_user_name=user, token=token)

    if pool == DEFAULT_WORKSPACE_POOL:
        if not secrets.github_token_default or not secrets.git_user_name_default:
            raise GithubAuthResolutionError(
                "Default workspace pool requires GITHUB_TOKEN_DEFAULT and GIT_USER_NAME_DEFAULT "
                "(platform secrets) when no per-key token is stored"
            )
        return GithubAuthContext(
            git_user_name=secrets.git_user_name_default,
            token=secrets.github_token_default,
        )

    raise GithubAuthResolutionError(
        f"Workspace pool {pool!r} requires a GitHub PAT: call PUT /api/v1/auth/github-token "
        f"with this API key before running git operations"
    )


def resolve_github_auth_for_generation_session_doc(db: IDatabase, est_doc: Dict[str, Any]) -> GithubAuthContext:
    """Load API key row by generation.key_uid and resolve PAT."""
    est_pool = est_doc.get("workspace_pool") or DEFAULT_WORKSPACE_POOL
    key_uid = est_doc.get("key_uid")

    if key_uid:
        secrets = get_github_platform_secrets()
        key_doc = db.get_api_key_by_uid(key_uid)
        if not key_doc:
            raise GithubAuthResolutionError(f"No API key found for key_uid={key_uid}")
        key_pool = key_doc.get("workspace_pool") or DEFAULT_WORKSPACE_POOL
        if key_pool != est_pool:
            raise GithubAuthResolutionError("API key workspace_pool does not match generation")
        return resolve_github_auth_for_api_key_document(key_doc, secrets)

    raise GithubAuthResolutionError(
        f"Generation {est_doc.get('generation_id')!r} is missing key_uid — "
        "migration may not have run (migrate_workspace_pool_key_uid.py)"
    )


def resolve_github_auth_for_generation_id(db: IDatabase, generation_id: str) -> GithubAuthContext:
    doc = db.get(COL_GENERATION_SESSIONS, generation_id)
    if not doc:
        raise GithubAuthResolutionError(f"Generation {generation_id} not found")
    return resolve_github_auth_for_generation_session_doc(db, doc)


def github_cli_env_for_generation(db: IDatabase, generation_id: str) -> dict[str, str]:
    """Env vars for ``gh`` in deploy-agent subprocesses (same PAT as git clone).

    Merged into ``agent_query`` env_config after the redaction overlay so the real
    value wins over pod ``GITHUB_TOKEN``/``GH_TOKEN`` masked as ``redacted``.
    """
    auth = resolve_github_auth_for_generation_id(db, generation_id)
    return {"GH_TOKEN": auth.token}
