"""Unit tests for the shared local-env helpers (services/local_env.py).

Pure logic (dotenv parse/write, repo-root walk, setup detection) is tested
directly; subprocess/HTTP-backed helpers are exercised with mocks so no docker
or backend is required.
"""

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import local_env

# ---------------------------------------------------------------------------
# parse_dotenv / read_dotenv / write_dotenv
# ---------------------------------------------------------------------------


class TestParseDotenv:
    def test_ignores_comments_and_blanks(self):
        text = "# comment\n\nKEY=value\n  # indented comment\nOTHER=2\n"
        assert local_env.parse_dotenv(text) == {"KEY": "value", "OTHER": "2"}

    def test_strips_whitespace_around_key_and_value(self):
        assert local_env.parse_dotenv("  KEY =  val  ") == {"KEY": "val"}

    def test_preserves_equals_in_value(self):
        assert local_env.parse_dotenv("URL=http://x/?a=b") == {"URL": "http://x/?a=b"}

    def test_last_value_wins(self):
        assert local_env.parse_dotenv("K=1\nK=2") == {"K": "2"}

    def test_skips_lines_without_equals(self):
        assert local_env.parse_dotenv("not an assignment\nK=1") == {"K": "1"}


class TestReadDotenv:
    def test_absent_returns_empty(self, tmp_path):
        assert local_env.read_dotenv(tmp_path) == {}

    def test_reads_existing(self, tmp_path):
        (tmp_path / ".env").write_text("A=1\nB=2\n")
        assert local_env.read_dotenv(tmp_path) == {"A": "1", "B": "2"}


class TestWriteDotenv:
    def test_creates_and_appends_new_keys(self, tmp_path):
        local_env.write_dotenv(tmp_path, {"A": "1", "B": "2"}, template_if_new=False)
        assert local_env.read_dotenv(tmp_path) == {"A": "1", "B": "2"}

    def test_replaces_in_place_preserving_comments_and_order(self, tmp_path):
        (tmp_path / ".env").write_text("# header\nA=old\n\n# mid\nB=keep\n")
        local_env.write_dotenv(tmp_path, {"A": "new"})
        text = (tmp_path / ".env").read_text()
        assert "# header" in text and "# mid" in text
        # A replaced in place (before B), B untouched.
        assert text.index("A=new") < text.index("B=keep")
        parsed = local_env.read_dotenv(tmp_path)
        assert parsed["A"] == "new" and parsed["B"] == "keep"

    def test_seeds_from_example_when_absent(self, tmp_path):
        (tmp_path / ".env.quickstart.example").write_text("# tmpl\nGITHUB_ORG=\nAUTH_MODE=local\n")
        local_env.write_dotenv(tmp_path, {"GITHUB_ORG": "acme"})
        text = (tmp_path / ".env").read_text()
        assert "# tmpl" in text
        parsed = local_env.read_dotenv(tmp_path)
        assert parsed["GITHUB_ORG"] == "acme"
        assert parsed["AUTH_MODE"] == "local"  # template scaffolding carried over

    def test_no_template_when_disabled(self, tmp_path):
        (tmp_path / ".env.quickstart.example").write_text("AUTH_MODE=local\n")
        local_env.write_dotenv(tmp_path, {"A": "1"}, template_if_new=False)
        assert local_env.read_dotenv(tmp_path) == {"A": "1"}


# ---------------------------------------------------------------------------
# repo_root / setup detection
# ---------------------------------------------------------------------------


class TestRepoRoot:
    def _make_repo(self, root: Path) -> None:
        (root / "specflow-init.sh").write_text("#!/bin/bash\n")
        (root / "docker-compose.yml").write_text("services: {}\n")

    def test_finds_root_from_nested_dir(self, tmp_path):
        self._make_repo(tmp_path)
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        assert local_env.repo_root(nested) == tmp_path

    def test_finds_root_at_start(self, tmp_path):
        self._make_repo(tmp_path)
        assert local_env.repo_root(tmp_path) == tmp_path

    def test_none_when_no_sentinels(self, tmp_path):
        nested = tmp_path / "x"
        nested.mkdir()
        assert local_env.repo_root(nested) is None

    def test_none_when_only_one_sentinel(self, tmp_path):
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        assert local_env.repo_root(tmp_path) is None


class TestResolveRepoRoot:
    def test_installed_repo_root_finds_real_checkout(self):
        # The test suite runs from the clone, so the package's own location
        # resolves to a checkout containing both sentinels.
        root = local_env.installed_repo_root()
        assert root is not None
        for name in local_env.SENTINEL_FILES:
            assert (root / name).exists()

    def test_resolve_prefers_cwd_walk_up(self, tmp_path):
        (tmp_path / "specflow-init.sh").write_text("#!/bin/bash\n")
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        # cwd walk-up wins even though installed_repo_root would also return one.
        assert local_env.resolve_repo_root(tmp_path) == tmp_path

    def test_resolve_falls_back_to_installed(self, tmp_path):
        nested = tmp_path / "not-a-checkout"
        nested.mkdir()
        with (
            patch.object(local_env, "repo_root", return_value=None),
            patch.object(local_env, "installed_repo_root", return_value=tmp_path),
        ):
            assert local_env.resolve_repo_root(nested) == tmp_path

    def test_resolve_none_when_neither(self, tmp_path):
        nested = tmp_path / "x"
        nested.mkdir()
        with (
            patch.object(local_env, "repo_root", return_value=None),
            patch.object(local_env, "installed_repo_root", return_value=None),
        ):
            assert local_env.resolve_repo_root(nested) is None


class TestSetupDetection:
    def test_is_setup_complete_requires_both(self, tmp_path):
        assert not local_env.is_setup_complete(tmp_path)
        (tmp_path / ".env").write_text("A=1\n")
        assert not local_env.is_setup_complete(tmp_path)
        cfg = tmp_path / ".specflow-local" / "mcp-config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{}")
        assert local_env.is_setup_complete(tmp_path)


# ---------------------------------------------------------------------------
# containers_running (mocked docker ps)
# ---------------------------------------------------------------------------


class TestContainersRunning:
    def _run(self, stdout: str, returncode: int = 0):
        from types import SimpleNamespace

        return SimpleNamespace(stdout=stdout, returncode=returncode)

    def test_both_present(self):
        out = "specflow-backend\nspecflow-firestore-emulator\n"
        with patch("services.local_env.subprocess.run", return_value=self._run(out)):
            assert local_env.containers_running() is True

    def test_one_missing(self):
        with patch(
            "services.local_env.subprocess.run", return_value=self._run("specflow-backend\n")
        ):
            assert local_env.containers_running() is False

    def test_nonzero_returncode(self):
        with patch("services.local_env.subprocess.run", return_value=self._run("", returncode=1)):
            assert local_env.containers_running() is False

    def test_docker_missing(self):
        with patch("services.local_env.subprocess.run", side_effect=FileNotFoundError):
            assert local_env.containers_running() is False


# ---------------------------------------------------------------------------
# backend_ready / wait_backend_ready (mocked httpx)
# ---------------------------------------------------------------------------


class TestBackendReady:
    @pytest.mark.asyncio
    async def test_ready_on_200(self):
        with patch("services.local_env.httpx.AsyncClient") as cls:
            client = cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=httpx.Response(200))
            assert await local_env.backend_ready("http://localhost:8000") is True

    @pytest.mark.asyncio
    async def test_not_ready_on_503(self):
        with patch("services.local_env.httpx.AsyncClient") as cls:
            client = cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=httpx.Response(503))
            assert await local_env.backend_ready("http://localhost:8000") is False

    @pytest.mark.asyncio
    async def test_not_ready_on_error(self):
        with patch("services.local_env.httpx.AsyncClient") as cls:
            client = cls.return_value.__aenter__.return_value
            client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            assert await local_env.backend_ready("http://localhost:8000") is False


class TestWaitBackendReady:
    @pytest.mark.asyncio
    async def test_returns_true_when_ready(self):
        with patch("services.local_env.backend_ready", new=AsyncMock(side_effect=[False, True])):
            ok = await local_env.wait_backend_ready("u", retries=5, interval=0)
        assert ok is True

    @pytest.mark.asyncio
    async def test_gives_up_after_retries(self):
        with patch("services.local_env.backend_ready", new=AsyncMock(return_value=False)):
            ok = await local_env.wait_backend_ready("u", retries=3, interval=0)
        assert ok is False


# ---------------------------------------------------------------------------
# InitFlags + run_init / start_containers (mocked subprocess)
# ---------------------------------------------------------------------------


class TestInitFlags:
    def test_empty_by_default(self):
        assert local_env.InitFlags().to_argv() == []

    def test_full_flags(self):
        flags = local_env.InitFlags(
            max_parallel_runs=2,
            skip_build=True,
            reset_local_db=True,
            provide_own_repos="a,b,c",
            dry_run=True,
        )
        assert flags.to_argv() == [
            "--max-parallel-runs",
            "2",
            "--skip-build",
            "--reset-local-db",
            "--provide-own-repos",
            "a,b,c",
            "--dry-run",
        ]


class _FakeProc:
    def __init__(self, lines: list[bytes], code: int = 0):
        self.stdout = self._aiter(lines)
        self._code = code

    @staticmethod
    async def _aiter(lines):
        for line in lines:
            yield line

    async def wait(self):
        return self._code


class TestRunInit:
    @pytest.mark.asyncio
    async def test_streams_lines_and_returns_code(self, tmp_path):
        captured: list[str] = []
        fake = _FakeProc([b"step 1\n", b"step 2\n"], code=0)
        with patch(
            "services.local_env.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake),
        ) as exec_mock:
            rc = await local_env.run_init(
                tmp_path, local_env.InitFlags(dry_run=True), on_line=captured.append
            )
        assert rc == 0
        assert captured == ["step 1\n", "step 2\n"]
        argv = exec_mock.call_args.args
        assert argv[0] == "bash"
        assert argv[1].endswith("specflow-init.sh")
        assert "--dry-run" in argv
        assert exec_mock.call_args.kwargs["cwd"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_start_containers_argv(self, tmp_path):
        fake = _FakeProc([], code=0)
        with patch(
            "services.local_env.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake),
        ) as exec_mock:
            rc = await local_env.start_containers(tmp_path)
        assert rc == 0
        assert list(exec_mock.call_args.args) == [
            "docker",
            "compose",
            "up",
            "-d",
            "--no-build",
        ]
        assert exec_mock.call_args.kwargs["cwd"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_exec_in_backend_wraps_docker_exec(self, tmp_path, monkeypatch):
        # docker exec <backend> <in-container argv...>, streamed, cwd = root.
        monkeypatch.setenv("SPECFLOW_BACKEND_CONTAINER", "specflow-backend-test")
        captured: list[str] = []
        fake = _FakeProc([b"line 1\n"], code=0)
        with patch(
            "services.local_env.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake),
        ) as exec_mock:
            rc = await local_env.exec_in_backend(
                tmp_path, ["sh", "/usr/local/bin/init-mobile-sdk.sh"], on_line=captured.append
            )
        assert rc == 0
        assert captured == ["line 1\n"]
        assert list(exec_mock.call_args.args) == [
            "docker",
            "exec",
            "specflow-backend-test",
            "sh",
            "/usr/local/bin/init-mobile-sdk.sh",
        ]
        assert exec_mock.call_args.kwargs["cwd"] == str(tmp_path)


class TestRunCommand:
    """run_command runs against real child processes — the point is to prove the
    timeout actually kills a hung process, which a mock cannot demonstrate."""

    @pytest.mark.asyncio
    async def test_captures_output_and_returncode_zero(self, tmp_path):
        captured: list[str] = []
        result = await local_env.run_command(
            [sys.executable, "-c", "print('hi')"],
            tmp_path,
            on_line=captured.append,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.timed_out is False
        assert result.ok is True
        assert result.output == "hi\n"
        assert captured == ["hi\n"]

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_not_ok(self, tmp_path):
        result = await local_env.run_command(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            tmp_path,
            timeout=10,
        )
        assert result.returncode == 3
        assert result.timed_out is False
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_timeout_kills_hung_process_quickly(self, tmp_path):
        start = time.monotonic()
        result = await local_env.run_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            tmp_path,
            timeout=0.5,
        )
        elapsed = time.monotonic() - start
        assert result.timed_out is True
        assert result.ok is False
        # Returned promptly after the timeout — the child was killed and reaped,
        # not waited out for the full 60s (which would hang the caller).
        assert elapsed < 10
