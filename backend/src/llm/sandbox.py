from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from claude_agent_sdk import SandboxNetworkConfig, SandboxSettings

DEFAULT_SANDBOX_ALLOWED_DOMAINS: tuple[str, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "proxy.golang.org",
    "sum.golang.org",
    "repo.maven.apache.org",
    "repo1.maven.org",
    "plugins.gradle.org",
    "services.gradle.org",
    "dl.google.com",
    "pub.dev",
    "github.com",
    "*.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "storage.googleapis.com",
)

_DEFAULT_EXCLUDED_COMMANDS = ("docker",)
_UNSAFE_PATH_CHARACTERS = frozenset("\n\r()[]*?!")


@dataclass(frozen=True, slots=True)
class NativeSandboxPolicy:
    """Caller-controlled policy for the SDK's native Bash sandbox."""

    allowed_domains: tuple[str, ...] = DEFAULT_SANDBOX_ALLOWED_DOMAINS
    excluded_commands: tuple[str, ...] = _DEFAULT_EXCLUDED_COMMANDS
    additional_write_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        domains = _validated_text_values(self.allowed_domains, "allowed_domains")
        commands = _validated_text_values(
            self.excluded_commands,
            "excluded_commands",
        )
        roots = tuple(_validated_write_root(root) for root in self.additional_write_roots)
        object.__setattr__(self, "allowed_domains", domains)
        object.__setattr__(self, "excluded_commands", commands)
        object.__setattr__(self, "additional_write_roots", roots)


type SandboxConfiguration = NativeSandboxPolicy | SandboxSettings | Literal[False]


@dataclass(frozen=True, slots=True)
class SandboxUnavailable:
    """Actionable native sandbox preflight failure."""

    platform: str
    dependencies: tuple[str, ...]
    message: str


class SandboxUnavailableError(RuntimeError):
    """Raised before an agent session when native isolation is unavailable."""

    def __init__(self, unavailable: SandboxUnavailable) -> None:
        super().__init__(unavailable.message)
        self.unavailable = unavailable


def check_native_sandbox_available(
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> SandboxUnavailable | None:
    """Return an actionable failure when host sandbox dependencies are absent."""

    current_platform = sys.platform if platform is None else platform
    if current_platform == "darwin":
        if which("sandbox-exec") is not None:
            return None
        return SandboxUnavailable(
            platform=current_platform,
            dependencies=("sandbox-exec",),
            message=(
                "macOS sandbox tool `sandbox-exec` was not found on PATH. "
                "It ships with macOS; ensure /usr/bin is on PATH."
            ),
        )
    if current_platform.startswith("linux"):
        missing = tuple(dependency for dependency in ("bwrap", "socat") if which(dependency) is None)
        if not missing:
            return None
        return SandboxUnavailable(
            platform=current_platform,
            dependencies=missing,
            message=(
                "Linux sandbox dependencies missing: "
                f"{', '.join(missing)}. Install bubblewrap and socat. "
                "Ubuntu 24.04+ may also require an AppArmor profile permitting "
                "unprivileged user namespaces for bwrap."
            ),
        )
    return SandboxUnavailable(
        platform=current_platform,
        dependencies=(),
        message=(
            "The native agent sandbox is unsupported on platform "
            f"{current_platform!r}. Pass sandbox=False only when an unconfined "
            "run is explicitly acceptable."
        ),
    )


def resolve_sandbox(
    configuration: SandboxConfiguration,
) -> tuple[SandboxSettings | None, tuple[str, ...]]:
    """Preflight and normalize sandbox settings plus extra writable roots."""

    if configuration is False:
        return None, ()

    unavailable = check_native_sandbox_available()
    if unavailable is not None:
        raise SandboxUnavailableError(unavailable)

    if isinstance(configuration, NativeSandboxPolicy):
        network: SandboxNetworkConfig = {
            "allowedDomains": list(configuration.allowed_domains),
        }
        settings = SandboxSettings(
            enabled=True,
            autoAllowBashIfSandboxed=True,
            allowUnsandboxedCommands=False,
            excludedCommands=list(configuration.excluded_commands),
            network=network,
        )
        rules = tuple(_edit_rule(root) for root in configuration.additional_write_roots)
        return settings, rules

    overrides: dict[str, Any] = dict(configuration)
    if overrides.get("enabled") is False:
        raise ValueError("sandbox settings cannot disable isolation; pass sandbox=False to opt out explicitly")
    if overrides.get("allowUnsandboxedCommands") is True:
        raise ValueError("allowUnsandboxedCommands=True violates fail-closed sandboxing")
    overrides["enabled"] = True
    overrides["allowUnsandboxedCommands"] = False
    return cast(SandboxSettings, overrides), ()


def _validated_text_values(
    values: tuple[str, ...],
    field_name: str,
) -> tuple[str, ...]:
    normalized = tuple(values)
    invalid = [value for value in normalized if not _is_trimmed_text(value)]
    if invalid:
        raise ValueError(f"{field_name} values must be non-empty strings without surrounding whitespace: {invalid!r}")
    return normalized


def _is_trimmed_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _validated_write_root(root: Path) -> Path:
    path = Path(root).expanduser().resolve()
    if path == Path(path.anchor):
        raise ValueError("additional sandbox write roots cannot be filesystem roots")
    unsupported = sorted(character for character in str(path) if character in _UNSAFE_PATH_CHARACTERS)
    if unsupported:
        raise ValueError(
            "additional sandbox write root contains characters unsupported by "
            "SDK permission rules: " + ", ".join(repr(character) for character in unsupported)
        )
    return path


def _edit_rule(root: Path) -> str:
    return f"Edit(//{root.as_posix().lstrip('/')}/**)"
