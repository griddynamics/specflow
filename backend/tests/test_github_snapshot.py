"""Tier 1: the GitHub clone check (docs/github/clone.md): git environment and argv, error codes, the snapshot
archive, the activity over fakes, and the workflow's last-resort paths.

A real ``git`` builds a local repository and clones it through a ``file://`` remote that only these tests inject;
production always clones ``https://github.com/{owner}/{name}.git``.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import ActivityEnvironment

from db.github import Snapshot
from github.client import SIZE_LIMIT_KB, Repository
from github.codes import CLONE_FAILURE_CODES, GitHubCode, GitHubError
from storage.keys import repository_snapshot
from storage.memory import InMemoryObjectStore
from temporal.agents import registration as agents_registration
from temporal.github import clone as git
from temporal.github import workflows as wf
from temporal.github.activities import GitHubSnapshotActivities, SnapshotOptions, Target
from temporal.github.models import (
    EXECUTION_TIMEOUT,
    WORKFLOW_NAME,
    GitHubSnapshotFailInput,
    GitHubSnapshotInput,
    GitHubSnapshotResult,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

GRANT = "ghu_fake-grant-value"  # a fake user access token the tests look for
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
PID, RID, USER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
DATA = GitHubSnapshotInput(project_id=PID, repository_id=RID)
CLONE_FAILED = "github_clone_failed"
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


GIT = shutil.which("git") or "git"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        [GIT, *args], cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **_COMMIT_ENV}
    )
    return done.stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A local repository: nested files, an executable, an in-tree link, an escaping link, an absolute link."""

    repo = tmp_path / "origin"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "README.md").write_text("# demo\n")
    (repo / "src" / "pkg" / "app.py").write_text("print('hi')\n")
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    (repo / "docs").mkdir()
    (repo / "docs" / "readme-link").symlink_to("../README.md")
    (repo / "escape").symlink_to("../../outside")
    (repo / "absolute").symlink_to("/etc/passwd")
    _git(repo, "init", "--quiet", "--initial-branch=trunk")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "one")
    return repo


def _file_remote(path: Path) -> git.Remote:
    return git.Remote(url=f"file://{path}", protocol="file", header_scope="file://")


# git environment and argv ------------------------------------------------------------------------------------------


def test_the_production_remote_is_github_over_https_only():
    remote = git.github_remote("octo/Hello-World.js")
    assert remote == git.Remote("https://github.com/octo/Hello-World.js.git", "https", "https://github.com/")


@pytest.mark.parametrize("name", ["octo", "octo/a/b", "../x", "octo/..", "octo/a b", "-x/y\n", "octo/a?b", ""])
def test_a_repository_name_outside_owner_slash_name_is_refused(name):
    with pytest.raises(GitHubError) as error:
        git.github_remote(name)
    assert error.value.code is GitHubCode.CLONE_FAILED


def test_the_token_reaches_git_only_as_a_scoped_header_in_the_environment(tmp_path):
    remote = git.github_remote("octo/repo")
    env = git.git_env(tmp_path, remote, GRANT, path="/usr/bin")
    argv = git.clone_argv(remote, tmp_path / "repo")

    assert all(GRANT not in part for part in argv)
    assert all("x-access-token" not in part for part in argv)
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    header = pairs.pop("http.https://github.com/.extraheader")
    assert header == "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{GRANT}".encode()).decode()
    assert pairs == {
        "core.hooksPath": "/dev/null",
        "core.fsmonitor": "false",
        "credential.helper": "",
        "protocol.allow": "never",
        "protocol.https.allow": "always",
        "submodule.recurse": "false",
    }
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["HOME"] == env["XDG_CONFIG_HOME"] == str(tmp_path)
    assert env["PATH"] == "/usr/bin"
    assert argv == [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",
        "--quiet",
        "--",
        "https://github.com/octo/repo.git",
        str(tmp_path / "repo"),
    ]


def test_the_environment_inherits_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PASSWORD", "db-secret")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "app-secret")
    env = git.git_env(tmp_path, git.github_remote("o/r"), None)
    assert "DATABASE_PASSWORD" not in env and "GITHUB_APP_CLIENT_SECRET" not in env
    assert not any("extraheader" in value for value in env.values())
    assert env["PATH"] == git.DEFAULT_PATH
    assert set(env) - {k for k in env if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))} == {
        "PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "LANG",
        "LC_ALL",
        "GIT_TERMINAL_PROMPT",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_LFS_SKIP_SMUDGE",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_COUNT",
    }


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (b"remote: Repository not found.\nfatal: repository 'https://github.com/o/r.git/' not found", "access"),
        (b"fatal: Authentication failed for 'https://github.com/o/r.git/'", "access"),
        (b"fatal: could not read Username for 'https://github.com': terminal prompts disabled", "access"),
        (b"fatal: unable to access '...': The requested URL returned error: 403", "access"),
        (b"fatal: unable to access '...': Could not resolve host: github.com", "network"),
        (b"fatal: unable to access '...': Failed to connect to github.com port 443", "network"),
        (b"error: RPC failed; curl 56 OpenSSL SSL_read: Connection reset by peer", "network"),
        (b"fatal: unable to access '...': The requested URL returned error: 502", "network"),
        (b"fatal: early EOF", "network"),
        (b"fatal: destination path exists", "other"),
        (b"", "other"),
    ],
)
def test_git_failures_become_closed_codes(stderr, code):
    expected = {
        "access": GitHubCode.REPO_NOT_ACCESSIBLE,
        "network": GitHubCode.UNREACHABLE,
        "other": GitHubCode.CLONE_FAILED,
    }[code]
    assert git.failure_code(stderr) is expected


class RecordingRunner:
    """A fake ``GitRunner``: records argv and env, answers in order."""

    def __init__(self, *answers: git.GitResult | BaseException) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], dict[str, str], Path | None, float]] = []

    async def __call__(self, argv, env, cwd, seconds):
        self.calls.append((list(argv), dict(env), cwd, seconds))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


SHA = "a" * 40


def _ok(stdout: bytes = b"") -> git.GitResult:
    return git.GitResult(0, stdout, b"")


async def test_clone_runs_git_with_the_token_only_in_the_clone_environment(tmp_path):
    runner = RecordingRunner(_ok(), _ok(SHA.encode() + b"\n"), _ok(b"main\n"))
    cloned = await git.clone(git.github_remote("o/r"), GRANT, tmp_path, runner=runner, seconds=5)

    assert cloned == git.Clone(path=tmp_path / "repo", head_sha=SHA, branch="main")
    (clone_argv, clone_env, clone_cwd, timeout), rev_parse, symbolic = runner.calls
    assert (clone_cwd, timeout) == (tmp_path, 5)
    headers = [value for value in clone_env.values() if value.startswith("AUTHORIZATION")]
    assert [base64.b64decode(value.split()[-1]).decode() for value in headers] == [f"x-access-token:{GRANT}"]
    for argv, _env, *_ in runner.calls:
        assert all(GRANT not in part for part in argv)
    for _argv, env, cwd, _timeout in (rev_parse, symbolic):
        assert cwd == tmp_path / "repo"
        assert not any(value.startswith("AUTHORIZATION") for value in env.values())
    assert rev_parse[0] == ["git", "rev-parse", "HEAD"]
    assert symbolic[0] == ["git", "symbolic-ref", "--quiet", "--short", "HEAD"]
    assert clone_argv[-2] == "https://github.com/o/r.git"


async def test_a_clone_timeout_and_a_failed_clone_are_closed_codes(tmp_path):
    (tmp_path / "a").mkdir()
    with pytest.raises(GitHubError) as timed_out:
        await git.clone(git.github_remote("o/r"), GRANT, tmp_path / "a", runner=RecordingRunner(TimeoutError()))
    assert timed_out.value.code is GitHubCode.CLONE_TIMEOUT

    denied = git.GitResult(128, b"", b"remote: Repository not found.")
    (tmp_path / "b").mkdir()
    with pytest.raises(GitHubError) as failed:
        await git.clone(git.github_remote("o/r"), GRANT, tmp_path / "b", runner=RecordingRunner(denied))
    assert failed.value.code is GitHubCode.REPO_NOT_ACCESSIBLE
    assert "not found" not in str(failed.value)


async def test_a_head_that_is_not_a_commit_id_fails_the_clone(tmp_path):
    runner = RecordingRunner(_ok(), _ok(b"not-a-sha\n"))
    with pytest.raises(GitHubError) as error:
        await git.clone(git.github_remote("o/r"), GRANT, tmp_path, runner=runner)
    assert error.value.code is GitHubCode.CLONE_FAILED


async def test_run_git_kills_the_process_group_on_a_timeout(tmp_path, monkeypatch):
    killed: list[int] = []
    real_kill = git._kill_group

    def record(pid: int) -> None:
        killed.append(pid)
        real_kill(pid)

    monkeypatch.setattr(git, "_kill_group", record)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError):
        await git.run_git(["sh", "-c", "sleep 30 & wait"], {"PATH": git.DEFAULT_PATH}, tmp_path, 0.3)
    assert loop.time() - started < 10  # the group is gone: wait() did not block on the grandchild
    assert len(killed) == 1


# The real clone and the archive ---------------------------------------------------------------------------------------


async def test_a_real_clone_gives_the_commit_branch_and_stats(origin, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    cloned = await git.clone(_file_remote(origin), None, work)
    assert cloned.head_sha == _git(origin, "rev-parse", "HEAD").strip()
    assert cloned.branch == "trunk"
    stats = git.tree_stats(cloned.path)
    sizes = len("# demo\n") + len("print('hi')\n") + len("#!/bin/sh\necho hi\n")
    assert stats == git.TreeStats(file_count=3, size_bytes=sizes, links_skipped=3)  # links are not files


def _names(archive: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(archive, "r:gz") as tar:
        return {member.name: member for member in tar.getmembers()}


async def test_the_snapshot_is_deterministic_and_safe(origin, tmp_path):
    archives = []
    for run in ("one", "two"):
        work = tmp_path / run
        work.mkdir()
        cloned = await git.clone(_file_remote(origin), None, work)
        archive = work / "s.tar.gz"
        size = git.write_snapshot(cloned.path, archive)
        assert size == archive.stat().st_size
        archives.append(archive.read_bytes())
    assert archives[0] == archives[1]

    members = _names(tmp_path / "one" / "s.tar.gz")
    assert sorted(members) == ["README.md", "run.sh", "src/pkg/app.py"]  # no symlink, even an in-tree one
    assert all(m.mtime == 0 and m.uid == 0 and m.gid == 0 and not m.uname and not m.gname for m in members.values())
    assert members["run.sh"].mode == 0o755 and members["README.md"].mode == 0o644
    assert all(member.isreg() for member in members.values())
    header = gzip.GzipFile(fileobj=io.BytesIO(archives[0]))
    header.read(1)
    assert header.mtime == 0


def test_the_snapshot_leaves_out_git_and_non_regular_files(tmp_path):
    root = tmp_path / "tree"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("[http]\nextraheader = secret\n")
    (root / "keep.txt").write_text("x")
    os.mkfifo(root / "pipe")
    (root / "dirlink").symlink_to("..")
    archive = tmp_path / "s.tar.gz"
    git.write_snapshot(root, archive)
    assert sorted(_names(archive)) == ["keep.txt"]
    assert git.tree_stats(root) == git.TreeStats(file_count=1, size_bytes=1, links_skipped=1)


def test_chained_links_that_resolve_out_of_the_tree_are_not_in_the_snapshot(tmp_path):
    """``p/q/a -> ../..`` and ``p/q/s -> a/..``: each looks harmless alone, together ``s`` leaves the root."""

    root = tmp_path / "tree"
    (root / "p" / "q").mkdir(parents=True)
    (root / "p" / "q" / "a").symlink_to("../..")
    (root / "p" / "q" / "s").symlink_to("a/..")
    (root / "p" / "q" / "file.txt").write_text("ok")
    archive = tmp_path / "s.tar.gz"
    git.write_snapshot(root, archive)

    assert sorted(_names(archive)) == ["p/q/file.txt"]
    assert git.tree_stats(root) == git.TreeStats(file_count=1, size_bytes=2, links_skipped=2)


@pytest.mark.parametrize(
    ("stats", "too_large"),
    [
        (git.TreeStats(file_count=git.MAX_TREE_FILES, size_bytes=git.MAX_TREE_BYTES), False),
        (git.TreeStats(file_count=1, size_bytes=git.MAX_TREE_BYTES + 1), True),
        (git.TreeStats(file_count=git.MAX_TREE_FILES + 1, size_bytes=1), True),
    ],
)
def test_the_checked_out_tree_has_hard_caps(stats, too_large):
    if not too_large:
        git.check_limits(stats)
        return
    with pytest.raises(GitHubError) as error:
        git.check_limits(stats)
    assert error.value.code is GitHubCode.REPO_TOO_LARGE


async def test_run_git_keeps_at_most_the_limit_of_a_pipe(tmp_path):
    loud = f"head -c {4 * git.STDERR_LIMIT} /dev/zero | tr '\\0' x >&2; echo done"
    result = await git.run_git(["sh", "-c", loud], {"PATH": git.DEFAULT_PATH}, tmp_path, 30)
    assert result.returncode == 0
    assert len(result.stderr) == git.STDERR_LIMIT and set(result.stderr) == {ord("x")}
    assert result.stdout == b"done\n"


# The activity ---------------------------------------------------------------------------------------------------------


class FakeRows:
    def __init__(self, target: Target | None = None, *, vanish_at_finish: bool = False, previous: str | None = None):
        self.target = target if target is not None else Target(added_by=USER, github_repo_id=7)
        self.gone = False
        self.vanish_at_finish = vanish_at_finish
        self.previous = previous
        self.events: list[tuple[str, Any]] = []

    async def start(self, project_id, repository_id):
        self.events.append(("start", (project_id, repository_id)))
        return None if self.gone else self.target

    async def finish(self, _project_id, _repository_id, snapshot: Snapshot, *, now):
        assert now == NOW
        self.events.append(("finish", snapshot))
        if self.vanish_at_finish:
            return False, None
        return True, self.previous

    async def fail(self, _project_id, _repository_id, code, *, now):
        assert now == NOW
        self.events.append(("fail", code))
        return not self.gone


class FakeConnections:
    def __init__(self, *, size_kb: int = 10, error: GitHubCode | None = None, full_name: str = "octo/demo"):
        self.size_kb, self.error, self.full_name = size_kb, error, full_name
        self.users: list[uuid.UUID] = []

    async def access_token(self, user_id):
        self.users.append(user_id)
        if self.error is not None:
            raise GitHubError(self.error)
        return GRANT

    async def repository(self, _user_id, repository_id):
        return Repository(
            repository_id,
            self.full_name,
            "https://github.com/x",
            private=True,
            default_branch="main",
            size_kb=self.size_kb,
        )


class FailingStore(InMemoryObjectStore):
    async def open_writer(self, _key, *, content_type):
        raise OSError(content_type)


def _activities(
    origin: Path | None,
    *,
    rows: FakeRows | None = None,
    connections: Any = "default",
    store: InMemoryObjectStore | None = None,
    runner: Any = None,
    tmp: Path,
) -> tuple[GitHubSnapshotActivities, FakeRows, InMemoryObjectStore]:
    rows = rows or FakeRows()
    store = store or InMemoryObjectStore(app_env="local")
    remotes: list[str] = []

    def remote(full_name: str) -> git.Remote:
        remotes.append(full_name)
        git.github_remote(full_name)  # the production rule still applies to the name
        return _file_remote(origin) if origin is not None else git.github_remote(full_name)

    options = SnapshotOptions(
        remote=remote, runner=runner or git.run_git, heartbeat_interval=0.01, temp_root=str(tmp), now=lambda: NOW
    )
    acts = GitHubSnapshotActivities(
        rows, store, FakeConnections() if connections == "default" else connections, options
    )
    return acts, rows, store


async def _run(acts: GitHubSnapshotActivities, *, attempt: int = 1) -> GitHubSnapshotResult:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=attempt)
    return await env.run(acts.github_snapshot, DATA)


def _temp_left(tmp: Path) -> list[str]:
    return [p.name for p in tmp.iterdir() if p.name.startswith("gh-snapshot-")]


async def test_a_check_stores_the_snapshot_and_marks_the_row_cloned(origin, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    acts, rows, store = _activities(origin, rows=FakeRows(previous="projects/p/repositories/r/old.tar.gz"), tmp=scratch)
    old = await store.open_writer("projects/p/repositories/r/old.tar.gz", content_type="application/gzip")
    await old.write(b"old")
    await old.close()

    result = await _run(acts)

    assert result == GitHubSnapshotResult(code=None, ended=True)
    (start, finish) = rows.events
    assert start[0] == "start"
    snapshot: Snapshot = finish[1]
    assert snapshot.default_branch == "trunk"
    assert snapshot.file_count == 3
    assert snapshot.snapshot_key == repository_snapshot(str(PID), str(RID), snapshot.head_sha)
    assert await store.exists(snapshot.snapshot_key)
    assert not await store.exists("projects/p/repositories/r/old.tar.gz")
    assert snapshot.snapshot_bytes > 0
    assert _temp_left(scratch) == []


async def test_a_repository_removed_during_the_check_drops_the_new_snapshot(origin, tmp_path):
    acts, rows, store = _activities(origin, rows=FakeRows(vanish_at_finish=True), tmp=tmp_path)
    result = await _run(acts)
    assert result == GitHubSnapshotResult(code=None, ended=False)
    snapshot: Snapshot = rows.events[-1][1]
    assert not await store.exists(snapshot.snapshot_key)


async def test_a_repository_gone_at_the_start_ends_quietly(tmp_path):
    rows = FakeRows()
    rows.gone = True
    acts, rows, _ = _activities(None, rows=rows, tmp=tmp_path)
    assert await _run(acts) == GitHubSnapshotResult(code=None, ended=False)
    assert [event for event, _ in rows.events] == ["start"]


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ({"rows": FakeRows(Target(added_by=None, github_repo_id=7))}, "github_not_connected"),
        ({"connections": None}, "github_auth_failed"),
        ({"connections": FakeConnections(size_kb=SIZE_LIMIT_KB + 1)}, "github_repo_too_large"),
        ({"connections": FakeConnections(error=GitHubCode.RECONNECT_REQUIRED)}, "github_reconnect_required"),
        ({"connections": FakeConnections(error=GitHubCode.GRANT_INVALID)}, "github_reconnect_required"),
        ({"connections": FakeConnections(error=GitHubCode.REFUSED)}, "github_auth_failed"),
        ({"connections": FakeConnections(error=GitHubCode.UNREACHABLE)}, "github_unreachable"),
        ({"connections": FakeConnections(full_name="octo/../x")}, "github_clone_failed"),
        ({"runner": RecordingRunner(git.GitResult(128, b"", b"Repository not found"))}, "github_repo_not_accessible"),
        ({"runner": RecordingRunner(TimeoutError())}, "github_clone_timeout"),
        ({"store": FailingStore(app_env="local")}, "storage_failed"),
    ],
)
async def test_every_closed_failure_is_recorded_and_returned(origin, tmp_path, setup, code, caplog):
    acts, rows, _ = _activities(origin, tmp=tmp_path, **setup)
    result = await _run(acts)
    assert result == GitHubSnapshotResult(code=code, ended=True)
    assert rows.events[-1] == ("fail", code)
    assert code in CLONE_FAILURE_CODES
    assert _temp_left(tmp_path) == []
    assert GRANT not in caplog.text and "octo/demo" not in caplog.text


async def test_a_tree_over_the_cap_after_the_clone_is_too_large(origin, tmp_path, monkeypatch):
    monkeypatch.setattr(git, "MAX_TREE_BYTES", 10)  # GitHub's ``size`` said small; the checkout is not
    acts, rows, store = _activities(origin, tmp=tmp_path)
    assert await _run(acts) == GitHubSnapshotResult(code="github_repo_too_large", ended=True)
    assert rows.events[-1] == ("fail", "github_repo_too_large")
    assert store.objects == {}
    assert _temp_left(tmp_path) == []


async def test_an_unexpected_error_is_retried_then_recorded_on_the_last_attempt(origin, tmp_path, monkeypatch):
    acts, rows, _ = _activities(origin, tmp=tmp_path)

    def broken(_root):
        raise RuntimeError("SELECT secret FROM somewhere")

    monkeypatch.setattr(git, "tree_stats", broken)
    with pytest.raises(ApplicationError) as error:
        await _run(acts, attempt=1)
    assert error.value.message == "internal_error" and error.value.__cause__ is None
    assert ("fail", "github_clone_failed") not in rows.events

    assert await _run(acts, attempt=wf.SNAPSHOT_MAX_ATTEMPTS) == GitHubSnapshotResult(
        code="github_clone_failed", ended=True
    )
    assert rows.events[-1] == ("fail", "github_clone_failed")
    assert _temp_left(tmp_path) == []


async def test_the_fail_activity_accepts_only_clone_codes(tmp_path):
    acts, rows, _ = _activities(None, tmp=tmp_path)
    env = ActivityEnvironment()
    await env.run(acts.github_snapshot_fail, GitHubSnapshotFailInput(project_id=PID, repository_id=RID, code="x"))
    await env.run(
        acts.github_snapshot_fail,
        GitHubSnapshotFailInput(project_id=PID, repository_id=RID, code="github_clone_timeout"),
    )
    assert rows.events == [("fail", "github_clone_failed"), ("fail", "github_clone_timeout")]


# The workflow -----------------------------------------------------------------------------------------------------


def test_the_workflow_module_imports_no_infrastructure_packages():
    code = (
        "import sys\n"
        "import temporal.github.workflows\n"
        "bad = [m for m in ('sqlalchemy', 'asyncpg', 'httpx', 'cryptography', 'settings', 'db', 'storage', 'github',"
        " 'temporal.github.activities', 'temporal.github.clone') if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_the_workflow_policies_fit_the_execution_timeout():
    definition = workflow._Definition.must_from_class(wf.GithubSnapshotWorkflow)
    assert definition.name == WORKFLOW_NAME
    assert definition.name == "GithubSnapshotWorkflow"
    assert wf.SNAPSHOT_RETRY.maximum_attempts == wf.SNAPSHOT_MAX_ATTEMPTS == 2
    assert wf.SNAPSHOT_START_TO_CLOSE * wf.SNAPSHOT_MAX_ATTEMPTS + wf.FAIL_START_TO_CLOSE < EXECUTION_TIMEOUT
    assert wf.SNAPSHOT_START_TO_CLOSE.total_seconds() > git.CLONE_TIMEOUT_SECONDS


class ScriptedActivities:
    def __init__(self, first: Any) -> None:
        self.first = first
        self.calls: list[tuple[str, Any]] = []

    async def __call__(self, name, arg, **_options):
        self.calls.append((name, arg))
        if name == wf.SNAPSHOT_ACTIVITY:
            if isinstance(self.first, BaseException):
                raise self.first
            return self.first
        return True


def _activity_error() -> ActivityError:
    return ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type=wf.SNAPSHOT_ACTIVITY,
        activity_id="1",
        retry_state=None,
    )


async def test_the_workflow_returns_the_activity_result(monkeypatch):
    scripted = ScriptedActivities(GitHubSnapshotResult(code="github_repo_too_large"))
    monkeypatch.setattr(workflow, "execute_activity", scripted)
    result = await wf.GithubSnapshotWorkflow().run(DATA)
    assert result.code == "github_repo_too_large"
    assert [name for name, _ in scripted.calls] == [wf.SNAPSHOT_ACTIVITY]


@pytest.mark.parametrize("failure", [_activity_error(), asyncio.CancelledError()])
async def test_a_failed_or_cancelled_check_still_ends_the_row_failed(monkeypatch, failure):
    scripted = ScriptedActivities(failure)
    monkeypatch.setattr(workflow, "execute_activity", scripted)
    expected = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else ApplicationError
    with pytest.raises(expected) as error:
        await wf.GithubSnapshotWorkflow().run(DATA)
    names = [name for name, _ in scripted.calls]
    assert names == [wf.SNAPSHOT_ACTIVITY, wf.FAIL_ACTIVITY]
    assert scripted.calls[1][1] == GitHubSnapshotFailInput(project_id=PID, repository_id=RID, code=CLONE_FAILED)
    if expected is ApplicationError:
        assert error.value.non_retryable and error.value.__cause__ is None


# Registration -----------------------------------------------------------------------------------------------------


class _Engine:
    async def dispose(self) -> None:
        self.disposed = True


async def test_the_agents_queue_registers_the_clone_check_only_with_a_store(monkeypatch):
    monkeypatch.setattr(agents_registration, "build_session_factory", lambda _engine: object())
    monkeypatch.setattr(agents_registration, "dispose_agent_session_factory", lambda _s: asyncio.sleep(0))
    without = agents_registration.build_registration(_Engine(), object())
    assert wf.GithubSnapshotWorkflow not in without.workflows

    store = InMemoryObjectStore(app_env="local")
    built = agents_registration.build_registration(_Engine(), object(), store=store)
    assert wf.GithubSnapshotWorkflow in built.workflows
    names = {getattr(a, "__temporal_activity_definition").name for a in built.activities}
    assert {wf.SNAPSHOT_ACTIVITY, wf.FAIL_ACTIVITY} <= names
    assert built.close is not None
    await built.close()


async def test_a_cancelled_check_kills_nothing_late_and_leaves_no_temp_dir(origin, tmp_path):
    acts, rows, _ = _activities(origin, tmp=tmp_path, runner=RecordingRunner(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await _run(acts)
    assert [event for event, _ in rows.events] == ["start"]  # the workflow ends the row, not the activity
    assert _temp_left(tmp_path) == []
