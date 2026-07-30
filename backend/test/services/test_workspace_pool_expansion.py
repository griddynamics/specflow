"""Tests for pool expansion: naming continuation, ordering, and failure containment.

The load-bearing property is that expansion never disturbs what already exists — no
renumbering of sets, no rewriting of live workspace documents — and never publishes a
workspace slot that allocation or estimation could not use.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.database.memory import InMemoryDatabase
from app.services.github_repo_provisioner import (
    GitHubProvisioningError,
    ProvisionedRepo,
)
from app.services.p10y_repository_discovery import P10YDiscoveryError
from app.services.workspace_pool_expansion import (
    MAX_SETS_PER_EXPANSION,
    ExpansionPhase,
    PoolExpansionRegistry,
    WorkspacePoolExpansionError,
    expand_pool,
    shrink_pool,
    resolve_naming_scheme,
    start_expansion,
    validate_expansion_request,
)
from app.services.workspace_pool_seeding import (
    derive_naming_scheme,
    split_repo_name,
    split_repo_url,
)


@pytest.fixture
def db():
    database = InMemoryDatabase()
    yield database
    database.clear()


def _seed_pool(db, sets: int, org: str = "acme", prefix: str = "specflow-workspace"):
    """Seed `sets` complete sets following the standard naming convention."""
    num = 1
    for set_number in range(1, sets + 1):
        for index in range(1, 4):
            db.set(
                "workspaces",
                f"ws-{set_number:02d}-{index}",
                {
                    "repo_url": f"https://github.com/{org}/{prefix}{num}",
                    "p10y_repository_id": 1000 + num,
                    "set_number": set_number,
                    "workspace_pool": "default",
                    "status": "available",
                    "clean_verified": True,
                },
            )
            num += 1


class TestRepoNameParsing:
    def test_split_repo_url(self):
        assert split_repo_url("https://github.com/acme/specflow-workspace7") == (
            "acme",
            "specflow-workspace7",
        )
        assert split_repo_url("https://github.com/acme/ws.git/") == ("acme", "ws")
        assert split_repo_url("nonsense") is None
        assert split_repo_url("") is None

    def test_split_repo_name(self):
        assert split_repo_name("specflow-workspace7") == ("specflow-workspace", 7)
        assert split_repo_name("generation-workspace12") == ("generation-workspace", 12)
        # No trailing number → no position can be derived.
        assert split_repo_name("my-repo") is None
        assert split_repo_name("") is None


class TestNamingSchemeDerivation:
    """New repos must continue the pool's own convention, never a configured default."""

    def test_continues_existing_numbering(self, db):
        _seed_pool(db, sets=3)
        scheme = derive_naming_scheme(db.query("workspaces", filters=[]))

        assert (scheme.github_org, scheme.prefix) == ("acme", "specflow-workspace")
        assert scheme.highest_repo_number == 9
        assert scheme.highest_set_number == 3
        assert scheme.next_set_number == 4

    def test_repo_names_for_new_sets_start_after_the_highest(self, db):
        _seed_pool(db, sets=3)
        scheme = derive_naming_scheme(db.query("workspaces", filters=[]))

        assert scheme.repo_names_for_sets(2) == [
            "specflow-workspace10",
            "specflow-workspace11",
            "specflow-workspace12",
            "specflow-workspace13",
            "specflow-workspace14",
            "specflow-workspace15",
        ]

    def test_prefix_is_inferred_not_defaulted(self, db):
        """A pool built with the script's default prefix must keep using it."""
        _seed_pool(db, sets=1, prefix="generation-workspace")
        scheme = derive_naming_scheme(db.query("workspaces", filters=[]))

        assert scheme.prefix == "generation-workspace"
        assert scheme.repo_names_for_sets(1)[0] == "generation-workspace4"

    def test_majority_wins_over_a_stray_entry(self, db):
        _seed_pool(db, sets=2)
        db.set(
            "workspaces",
            "ws-99-1",
            {
                "repo_url": "https://github.com/someone-else/hand-made1",
                "set_number": 99,
                "workspace_pool": "default",
                "status": "available",
            },
        )
        scheme = derive_naming_scheme(db.query("workspaces", filters=[]))

        assert (scheme.github_org, scheme.prefix) == ("acme", "specflow-workspace")

    def test_empty_pool_uses_defaults_when_available(self):
        scheme = derive_naming_scheme(
            [], default_github_org="acme", default_prefix="specflow-workspace"
        )
        assert scheme.next_set_number == 1
        assert scheme.repo_names_for_sets(1) == [
            "specflow-workspace1",
            "specflow-workspace2",
            "specflow-workspace3",
        ]

    def test_empty_pool_without_defaults_is_unresolvable(self):
        assert derive_naming_scheme([]) is None

    def test_resolve_raises_an_actionable_error_for_an_empty_pool(self, db, monkeypatch):
        monkeypatch.setattr("app.services.workspace_pool_expansion.settings.GITHUB_ORG", None)
        with pytest.raises(WorkspacePoolExpansionError) as exc:
            resolve_naming_scheme([])
        assert "GITHUB_ORG" in str(exc.value)


class TestRequestValidation:
    def test_rejects_zero_and_negative(self):
        for bad in (0, -1):
            with pytest.raises(WorkspacePoolExpansionError):
                validate_expansion_request(bad)

    def test_rejects_absurd_counts_before_touching_github(self):
        """A typo must not create hundreds of repositories."""
        with pytest.raises(WorkspacePoolExpansionError) as exc:
            validate_expansion_request(MAX_SETS_PER_EXPANSION + 1)
        assert str(MAX_SETS_PER_EXPANSION) in str(exc.value)

    def test_accepts_the_limit(self):
        validate_expansion_request(MAX_SETS_PER_EXPANSION)


def _fake_clients(monkeypatch, repo_id_start: int = 5000):
    """Stub GitHub + P10Y so expansion can run end to end without network."""
    created: list[str] = []

    async def fake_provision(client, repo_names, team_slug=None, delay=0, on_progress=None):
        created.extend(repo_names)
        return [
            ProvisionedRepo(
                name=n,
                full_name=f"acme/{n}",
                html_url=f"https://github.com/acme/{n}",
                already_existed=False,
            )
            for n in repo_names
        ]

    async def fake_await_ids(client, org_id, repo_names, **kwargs):
        return {name: repo_id_start + i for i, name in enumerate(repo_names)}

    async def fake_enable(client, org_id, repo_ids, **kwargs):
        return {rid: {"status": "Live"} for rid in repo_ids}

    monkeypatch.setattr(
        "app.services.workspace_pool_expansion.provision_repositories", fake_provision
    )
    monkeypatch.setattr(
        "app.services.workspace_pool_expansion.p10y_discovery.await_repository_ids",
        fake_await_ids,
    )
    monkeypatch.setattr(
        "app.services.workspace_pool_expansion.p10y_discovery.enable_metrics_and_wait",
        fake_enable,
    )
    monkeypatch.setattr(
        "app.services.workspace_pool_expansion._require_p10y_config",
        lambda: ("https://compass.test", "key", 7),
    )
    monkeypatch.setattr(
        "app.services.workspace_pool_expansion._require_github_token", lambda: "token"
    )
    return created


class TestExpandPool:
    @pytest.mark.asyncio
    async def test_adds_three_sets_to_a_three_set_pool(self, db, monkeypatch):
        """The headline case: 3 sets of 3, add 3 more → ws-04..06, repos 10..18."""
        _seed_pool(db, sets=3)
        created = _fake_clients(monkeypatch)
        registry = PoolExpansionRegistry()
        job = registry.new_job("default", 3)

        await expand_pool(db, job, github_client=object(), p10y_client=AsyncMock())

        assert job.phase is ExpansionPhase.DONE
        assert job.error is None
        assert job.set_numbers == [4, 5, 6]
        assert created == [f"specflow-workspace{n}" for n in range(10, 19)]
        assert job.workspaces_created == 9

        # New documents exist, correctly numbered and immediately allocatable.
        new_doc = db.get("workspaces", "ws-04-1")
        assert new_doc["repo_url"] == "https://github.com/acme/specflow-workspace10"
        assert new_doc["p10y_repository_id"] == 5000
        assert new_doc["status"] == "available"
        assert new_doc["clean_verified"] is True
        assert db.get("workspaces", "ws-06-3") is not None

    @pytest.mark.asyncio
    async def test_existing_sets_are_untouched(self, db, monkeypatch):
        """Seeding must never rewrite a live document back to available/unlocked."""
        _seed_pool(db, sets=2)
        db.update(
            "workspaces",
            "ws-01-1",
            {"status": "allocated", "locked_by": "gen_live", "clean_verified": False},
        )
        _fake_clients(monkeypatch)
        registry = PoolExpansionRegistry()
        job = registry.new_job("default", 1)

        await expand_pool(db, job, github_client=object(), p10y_client=AsyncMock())

        assert job.phase is ExpansionPhase.DONE
        held = db.get("workspaces", "ws-01-1")
        assert held["status"] == "allocated"
        assert held["locked_by"] == "gen_live"
        assert held["clean_verified"] is False

    @pytest.mark.asyncio
    async def test_seeding_happens_only_after_p10y_ids_exist(self, db, monkeypatch):
        """A slot must never be published without a P10Y id — estimation could not use it."""
        _seed_pool(db, sets=1)
        _fake_clients(monkeypatch)

        async def failing_ids(client, org_id, repo_names, **kwargs):
            raise P10YDiscoveryError("Compass never surfaced them")

        monkeypatch.setattr(
            "app.services.workspace_pool_expansion.p10y_discovery.await_repository_ids",
            failing_ids,
        )
        registry = PoolExpansionRegistry()
        job = registry.new_job("default", 1)

        await expand_pool(db, job, github_client=object(), p10y_client=AsyncMock())

        assert job.phase is ExpansionPhase.FAILED
        assert "Compass never surfaced them" in job.error
        # No half-published slots.
        assert db.get("workspaces", "ws-02-1") is None
        assert job.workspaces_created == 0

    @pytest.mark.asyncio
    async def test_github_failure_is_captured_on_the_job(self, db, monkeypatch):
        _seed_pool(db, sets=1)
        _fake_clients(monkeypatch)

        async def failing_provision(*args, **kwargs):
            raise GitHubProvisioningError("GitHub rejected creation: name already taken")

        monkeypatch.setattr(
            "app.services.workspace_pool_expansion.provision_repositories", failing_provision
        )
        registry = PoolExpansionRegistry()
        job = registry.new_job("default", 1)

        await expand_pool(db, job, github_client=object(), p10y_client=AsyncMock())

        assert job.phase is ExpansionPhase.FAILED
        assert "name already taken" in job.error
        assert db.get("workspaces", "ws-02-1") is None

    @pytest.mark.asyncio
    async def test_unexpected_error_is_contained_not_propagated(self, db, monkeypatch):
        """expand_pool runs detached; it must never raise into the event loop."""
        _seed_pool(db, sets=1)
        _fake_clients(monkeypatch)

        async def boom(*args, **kwargs):
            raise ValueError("something unforeseen")

        monkeypatch.setattr("app.services.workspace_pool_expansion.provision_repositories", boom)
        registry = PoolExpansionRegistry()
        job = registry.new_job("default", 1)

        await expand_pool(db, job, github_client=object(), p10y_client=AsyncMock())

        assert job.phase is ExpansionPhase.FAILED
        assert "something unforeseen" in job.error

    @pytest.mark.asyncio
    async def test_rerun_after_partial_seeding_is_idempotent(self, db, monkeypatch):
        """Re-running must complete the remainder, not duplicate or clobber."""
        _seed_pool(db, sets=1)
        _fake_clients(monkeypatch)
        registry = PoolExpansionRegistry()

        first = registry.new_job("default", 1)
        await expand_pool(db, first, github_client=object(), p10y_client=AsyncMock())
        assert first.workspaces_created == 3

        # A second expansion continues from set 3, leaving set 2 alone.
        second = registry.new_job("default", 1)
        await expand_pool(db, second, github_client=object(), p10y_client=AsyncMock())

        assert second.set_numbers == [3]
        assert second.workspaces_created == 3
        assert db.get("workspaces", "ws-02-1")["p10y_repository_id"] == 5000

    @pytest.mark.asyncio
    async def test_pool_scoping(self, db, monkeypatch):
        """Expanding one pool must not read another pool's numbering."""
        _seed_pool(db, sets=2)
        db.set(
            "workspaces",
            "ws-07-1",
            {
                "repo_url": "https://github.com/other/other-prefix99",
                "set_number": 7,
                "workspace_pool": "testpool",
                "status": "available",
            },
        )
        _fake_clients(monkeypatch)
        registry = PoolExpansionRegistry()
        job = registry.new_job("default", 1)

        await expand_pool(db, job, github_client=object(), p10y_client=AsyncMock())

        # Set 3, not 8 — the testpool row is invisible here.
        assert job.set_numbers == [3]


class TestRegistryAndStart:
    def test_refuses_a_second_concurrent_expansion_for_the_same_pool(self, db, monkeypatch):
        """Two runs would derive identical repo numbers and collide."""
        _seed_pool(db, sets=1)
        _fake_clients(monkeypatch)
        registry = PoolExpansionRegistry()

        async def scenario():
            first = start_expansion(db, registry, sets=1, workspace_pool="default")
            with pytest.raises(WorkspacePoolExpansionError) as exc:
                start_expansion(db, registry, sets=1, workspace_pool="default")
            assert first.job_id in str(exc.value)
            await asyncio.sleep(0)

        asyncio.run(scenario())

    def test_different_pools_may_expand_concurrently(self, db, monkeypatch):
        _seed_pool(db, sets=1)
        _fake_clients(monkeypatch)
        registry = PoolExpansionRegistry()

        async def scenario():
            start_expansion(db, registry, sets=1, workspace_pool="default")
            start_expansion(db, registry, sets=1, workspace_pool="testpool")
            await asyncio.sleep(0)

        asyncio.run(scenario())

    def test_job_ids_are_unique_and_retrievable(self):
        registry = PoolExpansionRegistry()
        a = registry.new_job("default", 1)
        b = registry.new_job("default", 1)
        assert a.job_id != b.job_id
        assert registry.get("nope") is None

    def test_terminal_jobs_stop_blocking_the_pool(self):
        registry = PoolExpansionRegistry()
        job = registry.new_job("default", 1)
        registry.register(job, task=None)

        assert registry.active_for_pool("default") is job
        job.phase = ExpansionPhase.DONE
        assert registry.active_for_pool("default") is None


class TestExpansionPhase:
    def test_terminal_phases(self):
        assert ExpansionPhase.DONE.is_terminal
        assert ExpansionPhase.FAILED.is_terminal
        assert not ExpansionPhase.QUEUED.is_terminal
        assert not ExpansionPhase.SEEDING.is_terminal


class TestShrinkPool:
    """Shrink removes pool rows only; GitHub repositories must survive."""

    @pytest.mark.asyncio
    async def test_removes_clean_idle_workspaces(self, db):
        _seed_pool(db, sets=2)

        result = await shrink_pool(db, ["ws-02-1", "ws-02-2", "ws-02-3"])

        assert (result["total"], result["success"], result["failed"]) == (3, 3, 0)
        assert db.get("workspaces", "ws-02-1") is None
        # Set 1 untouched.
        assert db.get("workspaces", "ws-01-1") is not None

    @pytest.mark.asyncio
    async def test_message_states_the_repo_is_kept(self, db):
        """An operator must not read 'removed' as 'my archived generations are gone'."""
        _seed_pool(db, sets=1)

        result = await shrink_pool(db, ["ws-01-1"])

        assert "GitHub repository is untouched" in result["details"][0]["message"]

    @pytest.mark.asyncio
    async def test_refuses_allocated_workspaces(self, db):
        _seed_pool(db, sets=1)
        db.update("workspaces", "ws-01-1", {"status": "allocated", "locked_by": "gen_live"})

        result = await shrink_pool(db, ["ws-01-1"])

        assert result["failed"] == 1
        assert "reclaim it first" in result["details"][0]["message"]
        # Crucially, still present.
        assert db.get("workspaces", "ws-01-1") is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["cleaning", "stuck"])
    async def test_refuses_cleaning_and_stuck(self, db, state):
        _seed_pool(db, sets=1)
        db.update("workspaces", "ws-01-1", {"status": state, "clean_verified": False})

        result = await shrink_pool(db, ["ws-01-1"])

        assert result["failed"] == 1
        assert db.get("workspaces", "ws-01-1") is not None

    @pytest.mark.asyncio
    async def test_refuses_unverified_available_workspaces(self, db):
        """Stale disk data means the repo still holds files — reclaim before removing."""
        _seed_pool(db, sets=1)
        db.update("workspaces", "ws-01-1", {"clean_verified": False})

        result = await shrink_pool(db, ["ws-01-1"])

        assert result["failed"] == 1
        assert "not clean-verified" in result["details"][0]["message"]
        assert db.get("workspaces", "ws-01-1") is not None

    @pytest.mark.asyncio
    async def test_missing_workspace_reported_not_raised(self, db):
        result = await shrink_pool(db, ["ws-99-9"])

        assert result["failed"] == 1
        assert result["details"][0]["message"] == "Workspace not found."

    @pytest.mark.asyncio
    async def test_partial_batch_reports_each_member(self, db):
        _seed_pool(db, sets=1)
        db.update("workspaces", "ws-01-2", {"status": "allocated"})

        result = await shrink_pool(db, ["ws-01-1", "ws-01-2", "ws-01-3"])

        assert (result["success"], result["failed"]) == (2, 1)
        assert db.get("workspaces", "ws-01-2") is not None
        assert db.get("workspaces", "ws-01-1") is None

    @pytest.mark.asyncio
    async def test_expansion_after_shrink_reuses_the_freed_numbering(self, db, monkeypatch):
        """Removing the top set and re-expanding must re-adopt the same repo names."""
        _seed_pool(db, sets=2)
        await shrink_pool(db, ["ws-02-1", "ws-02-2", "ws-02-3"])
        _fake_clients(monkeypatch)

        registry = PoolExpansionRegistry()
        job = registry.new_job("default", 1)
        await expand_pool(db, job, github_client=object(), p10y_client=AsyncMock())

        assert job.set_numbers == [2]
        assert db.get("workspaces", "ws-02-1")["repo_url"].endswith("specflow-workspace4")
