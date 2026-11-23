"""The hardened shallow clone and the snapshot of its working tree (docs/github/clone.md "Clone", "Snapshot").

No Temporal here: the activity drives these steps. The token reaches git only through ``GIT_CONFIG_*`` environment
entries (an ``http.extraHeader``), never argv, the remote URL, ``.git/config``, a log line or an error. git's stderr
is read only to pick a closed code and is never logged or returned. The process gets a minimal environment, so no
secret of the worker leaks into git or its helpers.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import os
import re
import signal
import stat
import tarfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from github.codes import GitHubCode, GitHubError

GITHUB_ORIGIN: Final = "https://github.com/"
FULL_NAME: Final = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
CLONE_TIMEOUT_SECONDS: Final = 600.0  # docs/github/clone.md: 10 minutes at most
COMMAND_TIMEOUT_SECONDS: Final = 30.0  # rev-parse and symbolic-ref on the local clone
STDERR_LIMIT: Final = 64 * 1024  # enough to pick a code; the rest is read and dropped
READ_CHUNK: Final = 64 * 1024
MAX_TREE_BYTES: Final = 1024**3  # the checked-out files, as the 1 GiB cap of GitHub's ``size``
MAX_TREE_FILES: Final = 200_000
DEFAULT_PATH: Final = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SNAPSHOT_CONTENT_TYPE: Final = "application/gzip"
_SHA: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FILE_MODE: Final = 0o644
_EXEC_MODE: Final = 0o755
_GZIP_LEVEL: Final = 6

# Lower-case pieces of git's stderr (git and libcurl texts, LANG=C) -> a closed code. Access first: a 403 or 404
# also says "unable to access".
_ACCESS_SIGNS: Final = (
    "repository not found",
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "permission denied",
    "returned error: 401",
    "returned error: 403",
    "returned error: 404",
    "access denied",
)
_NETWORK_SIGNS: Final = (
    "could not resolve host",
    "could not resolve proxy",
    "failed to connect",
    "couldn't connect",
    "connection refused",
    "connection reset",
    "connection timed out",
    "operation timed out",
    "network is unreachable",
    "ssl",
    "tls",
    "gnutls",
    "early eof",
    "rpc failed",
    "returned error: 429",
    "returned error: 5",
)


@dataclass(frozen=True)
class Remote:
    """Where git fetches from: ``url`` and the one protocol git may use. The header scope is the URL prefix the
    token header is bound to (git sends it only to that origin)."""

    url: str
    protocol: str
    header_scope: str


def github_remote(full_name: str) -> Remote:
    """``https://github.com/{full_name}.git``; ``full_name`` must be ``owner/name`` of safe characters."""

    if not FULL_NAME.fullmatch(full_name) or ".." in full_name:
        raise GitHubError(GitHubCode.CLONE_FAILED)
    return Remote(url=f"{GITHUB_ORIGIN}{full_name}.git", protocol="https", header_scope=GITHUB_ORIGIN)


def git_env(home: Path, remote: Remote, token: str | None, *, path: str = DEFAULT_PATH) -> dict[str, str]:
    """The whole environment of a git process: nothing is inherited, ``PATH`` is the system one (the image's git is
    ``/usr/bin/git``)."""

    config: list[tuple[str, str]] = [
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("credential.helper", ""),
        ("protocol.allow", "never"),
        (f"protocol.{remote.protocol}.allow", "always"),
        ("submodule.recurse", "false"),
    ]
    if token is not None:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        config.append((f"http.{remote.header_scope}.extraheader", f"AUTHORIZATION: basic {basic}"))
    env = {
        "PATH": path,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": str(len(config)),
    }
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def clone_argv(remote: Remote, target: Path) -> list[str]:
    """The clone command: the default branch, one commit, no tags, no submodules. No secret in it."""

    return [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",
        "--quiet",
        "--",
        remote.url,
        str(target),
    ]


def failure_code(stderr: bytes) -> GitHubCode:
    """The closed code of a failed clone, from git's stderr (read, never kept)."""

    text = stderr.decode("utf-8", "replace").lower()
    if any(sign in text for sign in _ACCESS_SIGNS):
        return GitHubCode.REPO_NOT_ACCESSIBLE
    if any(sign in text for sign in _NETWORK_SIGNS):
        return GitHubCode.UNREACHABLE
    return GitHubCode.CLONE_FAILED


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class GitRunner(Protocol):
    """Runs argv with env and cwd within a timeout; raises ``TimeoutError`` after killing the process group."""

    async def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: Path | None, seconds: float
    ) -> GitResult: ...


async def _bounded(stream: asyncio.StreamReader | None) -> bytes:
    """The first ``STDERR_LIMIT`` bytes of a pipe; the rest is read and dropped (git never blocks on a full pipe)."""

    kept = bytearray()
    if stream is None:
        return b""
    while chunk := await stream.read(READ_CHUNK):
        if len(kept) < STDERR_LIMIT:
            kept += chunk[: STDERR_LIMIT - len(kept)]
    return bytes(kept)


async def run_git(argv: Sequence[str], env: Mapping[str, str], cwd: Path | None, seconds: float) -> GitResult:
    """One git process in its own process group; a timeout or a cancellation kills the whole group. Each pipe keeps at
    most ``STDERR_LIMIT`` bytes."""

    process = await asyncio.create_subprocess_exec(
        *argv,
        env=dict(env),
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def finished() -> tuple[bytes, bytes]:
        out, err = await asyncio.gather(_bounded(process.stdout), _bounded(process.stderr))
        await process.wait()
        return out, err

    try:
        stdout, stderr = await asyncio.wait_for(finished(), seconds)
    except BaseException:  # TimeoutError, CancelledError: no git child may outlive the step
        _kill_group(process.pid)
        with suppress(Exception):
            await asyncio.shield(process.wait())
        raise
    return GitResult(process.returncode or 0, stdout, stderr)


def _kill_group(pid: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


@dataclass(frozen=True)
class Clone:
    """A finished clone: the working tree, the commit and the branch."""

    path: Path
    head_sha: str
    branch: str | None


async def clone(
    remote: Remote,
    token: str | None,
    workdir: Path,
    *,
    runner: GitRunner = run_git,
    seconds: float = CLONE_TIMEOUT_SECONDS,
) -> Clone:
    """Clone into ``workdir/repo`` (``workdir`` must be empty and private). ``GitHubError`` with a closed code."""

    home = workdir / "home"
    home.mkdir(mode=0o700)
    target = workdir / "repo"
    env = git_env(home, remote, token)
    try:
        result = await runner(clone_argv(remote, target), env, workdir, seconds)
    except TimeoutError:
        raise GitHubError(GitHubCode.CLONE_TIMEOUT) from None
    if result.returncode != 0:
        raise GitHubError(failure_code(result.stderr))
    local = git_env(home, remote, None)  # the local commands need no token
    head = await runner(["git", "rev-parse", "HEAD"], local, target, COMMAND_TIMEOUT_SECONDS)
    sha = head.stdout.decode("ascii", "replace").strip()
    if head.returncode != 0 or not _SHA.fullmatch(sha):
        raise GitHubError(GitHubCode.CLONE_FAILED)
    ref = await runner(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], local, target, COMMAND_TIMEOUT_SECONDS)
    branch = ref.stdout.decode("utf-8", "replace").strip() if ref.returncode == 0 else ""
    return Clone(path=target, head_sha=sha, branch=branch or None)


@dataclass(frozen=True)
class TreeStats:
    """The regular files of a working tree (no ``.git``) and the symlinks left out of it."""

    file_count: int
    size_bytes: int
    links_skipped: int = 0


def _walk(root: Path) -> tuple[list[Path], int]:
    """Every regular file under ``root`` except ``.git``, sorted by path, and the number of symlinks left out. Never
    follows a link: a symlink (to a file or a directory) is not part of a snapshot at all, so no chain of links can
    point a reader out of the tree."""

    found: list[Path] = []
    links = 0
    for directory, names, files in os.walk(root, followlinks=False):
        here = Path(directory)
        if here == root:
            names[:] = [name for name in names if name != ".git"]
        names.sort()
        links += sum(1 for name in names if (here / name).is_symlink())  # listed with the directories, not walked
        for name in files:
            mode = (here / name).lstat().st_mode
            if stat.S_ISREG(mode):
                found.append(here / name)
            elif stat.S_ISLNK(mode):
                links += 1
    return sorted(found, key=lambda path: path.relative_to(root).as_posix()), links


def tree_stats(root: Path) -> TreeStats:
    """Count and bytes of the regular files of the working tree; the symlinks it leaves out."""

    files, links = _walk(root)
    return TreeStats(file_count=len(files), size_bytes=sum(path.lstat().st_size for path in files), links_skipped=links)


def check_limits(stats: TreeStats) -> None:
    """``github_repo_too_large`` when the checked-out tree is over the caps (GitHub's ``size`` field lags)."""

    if stats.size_bytes > MAX_TREE_BYTES or stats.file_count > MAX_TREE_FILES:
        raise GitHubError(GitHubCode.REPO_TOO_LARGE)


def _tar_info(root: Path, path: Path) -> tarfile.TarInfo | None:
    name = path.relative_to(root).as_posix()
    if name.startswith("/") or ".." in name.split("/"):
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):  # a file replaced since the walk
        return None
    member = tarfile.TarInfo(name)
    member.mtime = 0
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.type = tarfile.REGTYPE
    member.size = info.st_size
    member.mode = _EXEC_MODE if info.st_mode & stat.S_IXUSR else _FILE_MODE
    return member


def write_snapshot(root: Path, destination: Path) -> int:
    """A deterministic ``tar.gz`` of the working tree at ``destination``: regular files only, sorted, mtime 0, uid/gid
    0, no ``.git``, no symlink, no absolute or ``..`` names. Blocking; returns its size in bytes."""

    files, _links = _walk(root)
    with (
        destination.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=_GZIP_LEVEL) as packed,
        tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path in files:
            member = _tar_info(root, path)
            if member is not None:
                with path.open("rb") as content:
                    archive.addfile(member, content)
    return destination.stat().st_size
