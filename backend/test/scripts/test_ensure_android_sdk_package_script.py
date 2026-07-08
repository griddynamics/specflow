"""Tests for the additive Android SDK package wrapper."""

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ensure-android-sdk-package.sh"


def _base_env(tmp_path: Path, allow: str = "true") -> dict[str, str]:
    sdk_root = tmp_path / "caches" / "common" / "android"
    sdkmanager = sdk_root / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    sdkmanager.parent.mkdir(parents=True)
    sdkmanager.write_text(
        "#!/usr/bin/env sh\n"
        "echo \"$@\" >> \"${SDKMANAGER_LOG}\"\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    platforms\\;android-*) mkdir -p \"${ANDROID_SDK_ROOT}/platforms/${arg#platforms;}\" ;;\n"
        "    build-tools\\;*) mkdir -p \"${ANDROID_SDK_ROOT}/build-tools/${arg#build-tools;}\" ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    sdkmanager.chmod(0o755)
    return {
        **os.environ,
        "ALLOW_AGENT_SDKMANAGER": allow,
        "WORKSPACE_BASE_PATH": str(tmp_path),
        "ANDROID_SDK_ROOT": str(sdk_root),
        "SDKMANAGER_LOG": str(tmp_path / "sdkmanager.log"),
    }


def test_disabled_by_default_even_when_sdkmanager_exists(tmp_path: Path) -> None:
    env = _base_env(tmp_path, allow="false")

    result = subprocess.run(
        ["sh", str(SCRIPT), "platforms;android-33"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "disabled" in result.stderr
    assert not (tmp_path / "sdkmanager.log").exists()


def test_skips_existing_package_without_calling_sdkmanager(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    (Path(env["ANDROID_SDK_ROOT"]) / "platforms" / "android-33").mkdir(parents=True)

    result = subprocess.run(
        ["sh", str(SCRIPT), "platforms;android-33"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "already present" in result.stdout
    assert not (tmp_path / "sdkmanager.log").exists()


def test_installs_missing_allowed_package_additively(tmp_path: Path) -> None:
    env = _base_env(tmp_path)

    result = subprocess.run(
        ["sh", str(SCRIPT), "platforms;android-33", "build-tools;33.0.2"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    log = (tmp_path / "sdkmanager.log").read_text(encoding="utf-8")
    assert "--sdk_root=" in log
    assert "platforms;android-33" in log
    assert "build-tools;33.0.2" in log


def test_refuses_disallowed_package_id(tmp_path: Path) -> None:
    env = _base_env(tmp_path)

    result = subprocess.run(
        ["sh", str(SCRIPT), "cmdline-tools;latest"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "not allowed" in result.stderr
    assert not (tmp_path / "sdkmanager.log").exists()


def test_refuses_non_shared_sdk_root(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["ANDROID_SDK_ROOT"] = str(tmp_path / "workspace" / "android-sdk-local")

    result = subprocess.run(
        ["sh", str(SCRIPT), "platforms;android-33"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "outside shared cache" in result.stderr
