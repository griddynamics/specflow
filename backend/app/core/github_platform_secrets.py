"""
In-process GitHub platform credentials (default org PAT + Fernet key material).

Production: load from Kubernetes API once at startup (not pod-wide env).
Local/CI: TOKEN_ENCRYPTION_KEY, GITHUB_TOKEN_DEFAULT, GIT_USER_NAME_DEFAULT (or legacy GITHUB_TOKEN / GIT_USER_NAME).
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings

logger = logging.getLogger(__name__)

_secrets: Optional["GithubPlatformSecrets"] = None


@dataclass
class GithubPlatformSecrets:
    """Holds Fernet + default pool Git identity. Never log field values."""

    _fernet: Fernet
    github_token_default: Optional[str]
    git_user_name_default: Optional[str]

    def encrypt_token(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def decrypt_token(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as e:
            raise ValueError("GitHub token decryption failed") from e


def get_github_platform_secrets() -> GithubPlatformSecrets:
    if _secrets is None:
        raise RuntimeError(
            "GitHub platform secrets not initialized — call init_github_platform_secrets() "
            "during application startup"
        )
    return _secrets


def reset_github_platform_secrets() -> None:
    """Test hook: clear loaded secrets."""
    global _secrets
    _secrets = None


def init_github_platform_secrets_for_tests(
    *,
    fernet_key: bytes,
    github_token_default: str | None = "test-default-token",
    git_user_name_default: str | None = "test-user",
) -> None:
    """Initialize secrets for unit tests (no K8s / .env)."""
    global _secrets
    _secrets = GithubPlatformSecrets(
        _fernet=Fernet(fernet_key),
        github_token_default=github_token_default,
        git_user_name_default=git_user_name_default,
    )


def _decode_secret_value(raw: str | bytes) -> str:
    if isinstance(raw, bytes):
        return raw.decode()
    return raw


def _load_from_kubernetes(settings: Settings) -> dict[str, str]:
    from kubernetes import client, config

    config.load_incluster_config()
    v1 = client.CoreV1Api()
    secret = v1.read_namespaced_secret(
        name=settings.K8S_SECRET_NAME,
        namespace=settings.K8S_SECRET_NAMESPACE,
    )
    out: dict[str, str] = {}
    if secret.data:
        for k, v in secret.data.items():
            out[k] = base64.b64decode(v).decode()
    return out


def _build_secrets_from_map(
    data: dict[str, str],
    encryption_key_field: str,
    github_default_field: str,
    git_user_field: str,
) -> GithubPlatformSecrets:
    enc_raw = data.get(encryption_key_field)
    if not enc_raw:
        raise ValueError(
            f"Kubernetes secret missing encryption key field {encryption_key_field!r}"
        )
    fernet = Fernet(_decode_secret_value(enc_raw).strip().encode())
    gh = data.get(github_default_field)
    gu = data.get(git_user_field)
    return GithubPlatformSecrets(
        _fernet=fernet,
        github_token_default=gh.strip() if gh else None,
        git_user_name_default=gu.strip() if gu else None,
    )


def _load_from_env(settings: Settings) -> GithubPlatformSecrets:
    enc = settings.TOKEN_ENCRYPTION_KEY
    if not enc:
        raise ValueError(
            "TOKEN_ENCRYPTION_KEY is required (Fernet key) when not loading from Kubernetes"
        )
    fernet = Fernet(str(enc).strip().encode())
    gh_default = settings.GITHUB_TOKEN_DEFAULT
    git_user = settings.GIT_USER_NAME_DEFAULT
    return GithubPlatformSecrets(
        _fernet=fernet,
        github_token_default=gh_default.strip() if gh_default else None,
        git_user_name_default=git_user.strip() if git_user else None,
    )


def init_github_platform_secrets(settings: Settings) -> None:
    """
    Load platform secrets once. Prefer in-cluster Kubernetes when KUBERNETES_SERVICE_HOST is set
    and K8S_SECRET_NAME / K8S_SECRET_NAMESPACE are configured; otherwise use env-based loading.
    """
    global _secrets
    in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
    use_k8s = (
        in_cluster
        and bool(settings.K8S_SECRET_NAME)
        and bool(settings.K8S_SECRET_NAMESPACE)
    )
    if use_k8s:
        try:
            raw = _load_from_kubernetes(settings)
            _secrets = _build_secrets_from_map(
                raw,
                encryption_key_field=settings.K8S_SECRET_KEY_ENCRYPTION,
                github_default_field=settings.K8S_SECRET_KEY_GITHUB_DEFAULT,
                git_user_field=settings.K8S_SECRET_KEY_GIT_USER_DEFAULT,
            )
            logger.info(
                "Loaded GitHub platform secrets from Kubernetes API (namespace=%s, name=%s)",
                settings.K8S_SECRET_NAMESPACE,
                settings.K8S_SECRET_NAME,
            )
            return
        except Exception as e:
            logger.error("Failed to load platform secrets from Kubernetes: %s", e, exc_info=True)
            raise

    _secrets = _load_from_env(settings)
    logger.info("Loaded GitHub platform secrets from environment (local/CI path)")
