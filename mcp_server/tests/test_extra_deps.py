"""Tests for the extra-dependency script catalog (tui.extra_deps)."""

from tui.extra_deps import (
    EXTRA_DEPENDENCY_SCRIPTS,
    MOBILE_SDK_SCRIPT,
    DependencyComponent,
    ExtraDependencyScript,
    script_by_key,
)


def test_version_summary_joins_components():
    script = ExtraDependencyScript(
        key="demo",
        title="Demo",
        description="d",
        components=(
            DependencyComponent("Foo", "1.2"),
            DependencyComponent("Bar", "3.4"),
        ),
        container_path="/usr/local/bin/x.sh",
    )
    assert script.version_summary == "Foo 1.2, Bar 3.4"


def test_command_is_sh_plus_container_path():
    assert MOBILE_SDK_SCRIPT.command() == ["sh", "/usr/local/bin/init-mobile-sdk.sh"]


def test_mobile_sdk_advertises_android_and_flutter():
    summary = MOBILE_SDK_SCRIPT.version_summary
    assert "Android SDK" in summary
    assert "Flutter" in summary


def test_script_by_key_roundtrip():
    for script in EXTRA_DEPENDENCY_SCRIPTS:
        assert script_by_key(script.key) is script


def test_script_by_key_unknown_returns_none():
    assert script_by_key("does-not-exist") is None


def test_keys_are_unique():
    keys = [script.key for script in EXTRA_DEPENDENCY_SCRIPTS]
    assert len(keys) == len(set(keys))
