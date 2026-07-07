"""Scenario tests for GitHub PAT resolution (pool + per-key ciphertext)."""

import pytest
from cryptography.fernet import Fernet

from app.core.github_platform_secrets import (
    GithubPlatformSecrets,
    get_github_platform_secrets,
    init_github_platform_secrets_for_tests,
    reset_github_platform_secrets,
)
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.services.git_provider import GitProvider
from app.database.memory import InMemoryDatabase
from app.services.github_auth import (
    GithubAuthResolutionError,
    github_cli_env_for_generation,
    resolve_github_auth_for_api_key_document,
    resolve_github_auth_for_generation_session_doc,
)
from app.state.db_adapter import COL_GENERATION_SESSIONS


@pytest.fixture
def secrets():
    key = Fernet.generate_key()
    init_github_platform_secrets_for_tests(
        fernet_key=key,
        github_token_default="platform-default-pat",
        git_user_name_default="platform-git-user",
    )
    yield get_holder()
    reset_github_platform_secrets()


def get_holder() -> GithubPlatformSecrets:
    return get_github_platform_secrets()


@pytest.fixture
def db():
    d = InMemoryDatabase()
    yield d
    d.clear()


def test_default_pool_uses_platform_pat_when_no_ciphertext(db, secrets):
    doc = {"workspace_pool": DEFAULT_WORKSPACE_POOL}
    ctx = resolve_github_auth_for_api_key_document(doc, secrets)
    assert ctx.token == "platform-default-pat"
    assert ctx.git_user_name == "platform-git-user"


def test_dedicated_pool_requires_upload(db, secrets):
    doc = {"workspace_pool": "hf"}
    with pytest.raises(GithubAuthResolutionError):
        resolve_github_auth_for_api_key_document(doc, secrets)


def test_per_key_ciphertext_overrides_default_pool(db, secrets):
    s = get_holder()
    ct = s.encrypt_token("per-key-pat-value")
    doc = {
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        "github_token_ciphertext": ct,
        "git_user_name": "custom-u",
    }
    ctx = resolve_github_auth_for_api_key_document(doc, secrets)
    assert ctx.token == "per-key-pat-value"
    assert ctx.git_user_name == "custom-u"


def test_generation_resolves_via_key_uid(db, secrets):
    s = get_holder()
    ct = s.encrypt_token("hf-pat")
    db.set(
        "api_keys",
        "gain_k1",
        {
            "api_key": "gain_k1",
            "key_uid": "uid-1",
            "workspace_pool": "hf",
            "user_id": "u@u.com",
            "github_token_ciphertext": ct,
        },
    )
    db.set(
        COL_GENERATION_SESSIONS,
        "est-1",
        {
            "generation_id": "est-1",
            "workspace_pool": "hf",
            "key_uid": "uid-1",
        },
    )
    ctx = resolve_github_auth_for_generation_session_doc(db, db.get(COL_GENERATION_SESSIONS, "est-1"))
    assert ctx.token == "hf-pat"


def test_decrypt_failure_no_fallback(db, secrets):
    doc = {
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        "github_token_ciphertext": "invalid-fernet-payload",
    }
    with pytest.raises(GithubAuthResolutionError, match="decrypt"):
        resolve_github_auth_for_api_key_document(doc, secrets)


def test_generation_without_key_uid_raises_informative_error(db, secrets):
    """Phase 2: legacy generation path removed — missing key_uid → error with migration hint."""
    est_doc = {
        "generation_id": "est-old",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        # no key_uid
    }
    with pytest.raises(GithubAuthResolutionError, match="missing key_uid"):
        resolve_github_auth_for_generation_session_doc(db, est_doc)


def test_generation_key_uid_lookup_uses_adapter(db, secrets):
    """Phase 0: resolve_github_auth_for_generation_session_doc uses get_api_key_by_uid, not raw query."""
    s = secrets
    ct = s.encrypt_token("mytoken")
    db.set("api_keys", "gain_k99", {
        "api_key": "gain_k99",
        "key_uid": "uid-99",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        "github_token_ciphertext": ct,
    })
    est_doc = {
        "generation_id": "est-99",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        "key_uid": "uid-99",
    }

    # Ensure raw query on api_keys is NOT called for key_uid lookup
    original_query = db.query
    query_calls = []

    def patched_query(collection, filters=None, **kwargs):
        if collection == "api_keys" and any(
            f[0] == "key_uid" for f in (filters or [])
        ):
            query_calls.append(True)
        return original_query(collection, filters, **kwargs)

    db.query = patched_query

    ctx = resolve_github_auth_for_generation_session_doc(db, est_doc)
    assert ctx.token == "mytoken"
    assert len(query_calls) == 0, "Should not use raw query for key_uid lookup"


def test_default_pool_uses_bitbucket_token_and_user_when_active(db):
    key = Fernet.generate_key()
    init_github_platform_secrets_for_tests(
        fernet_key=key,
        github_token_default=None,
        git_user_name_default="x-token-auth",
        bitbucket_token_default="bb-platform-token",
        active_provider=GitProvider.BITBUCKET_CLOUD,
    )
    try:
        s = get_holder()
        doc = {"workspace_pool": DEFAULT_WORKSPACE_POOL}
        ctx = resolve_github_auth_for_api_key_document(doc, s)
        assert ctx.token == "bb-platform-token"
        assert ctx.git_user_name == "x-token-auth"
    finally:
        reset_github_platform_secrets()


def test_per_key_ciphertext_defaults_git_user_from_active_provider_strategy(db):
    """When active=BitBucket and no git_user_name is stored, default to x-token-auth."""
    key = Fernet.generate_key()
    init_github_platform_secrets_for_tests(
        fernet_key=key,
        github_token_default=None,
        git_user_name_default=None,
        bitbucket_token_default="bb-platform-token",
        active_provider=GitProvider.BITBUCKET_CLOUD,
    )
    try:
        s = get_holder()
        ct = s.encrypt_token("per-key-bb-token")
        doc = {
            "workspace_pool": DEFAULT_WORKSPACE_POOL,
            "github_token_ciphertext": ct,
        }
        ctx = resolve_github_auth_for_api_key_document(doc, s)
        assert ctx.token == "per-key-bb-token"
        assert ctx.git_user_name == "x-token-auth"
    finally:
        reset_github_platform_secrets()


def test_github_cli_env_for_generation(db, secrets):
    db.set(COL_GENERATION_SESSIONS, "gen-1", {
        "generation_id": "gen-1",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
        "key_uid": "uid-default",
    })
    db.set("api_keys", "key-1", {
        "key_uid": "uid-default",
        "workspace_pool": DEFAULT_WORKSPACE_POOL,
    })
    assert github_cli_env_for_generation(db, "gen-1") == {
        "GH_TOKEN": "platform-default-pat",
    }
