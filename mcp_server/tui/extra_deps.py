"""Catalog of optional "extra dependency" provisioning scripts.

Small runtimes (Node/npm, JDK, Gradle, Kotlin) are baked into the backend Docker
image, but the heavy mobile SDKs (Android SDK, Flutter — which bundles Dart) are
impractical to bake and are provisioned on demand into the shared NFS cache by
``backend/scripts/init-mobile-sdk.sh`` (copied to ``/usr/local/bin`` in the image).

A user who forgets that step finds the agents missing those SDKs mid-run. This
module models each such script as data so the Settings screen can list it with a
one-click install; adding another provisioning script is a single entry here
(Open/Closed) and inherits the render + docker-exec plumbing.

The version strings are display-only and MIRROR the env-overridable defaults in
``backend/scripts/init-mobile-sdk.sh`` (and ``backend/Dockerfile``), which remain
the single source of truth for what actually gets installed. This client is a
separate deployable and cannot import the backend script; a drift here only
mislabels the button, never changes what the script installs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DependencyComponent:
    """One installable piece advertised by a script (name + display version)."""

    name: str
    version: str

    def __str__(self) -> str:
        return f"{self.name} {self.version}"


@dataclass(frozen=True)
class ExtraDependencyScript:
    """An idempotent provisioning script baked into the backend container.

    ``container_path`` is the absolute path of the script inside the running
    backend container; ``command`` returns the argv to run it there (the caller
    prepends ``docker exec <backend>``). The script is idempotent, so re-running
    an already-provisioned component is a fast no-op.
    """

    key: str
    title: str
    description: str
    components: tuple[DependencyComponent, ...]
    container_path: str

    @property
    def version_summary(self) -> str:
        """Comma-joined "Name version" list shown next to the title."""
        return ", ".join(str(component) for component in self.components)

    def command(self) -> list[str]:
        """In-container argv that runs this script (interpreter + path)."""
        return ["sh", self.container_path]


# The mobile SDK provisioner — the one script we currently expose. Versions mirror
# init-mobile-sdk.sh: ANDROID_SDK_CMDLINE_TOOLS_VERSION and FLUTTER_VERSION.
MOBILE_SDK_SCRIPT = ExtraDependencyScript(
    key="mobile-sdk",
    title="Mobile SDKs",
    description=(
        "Android SDK, Flutter, and the bundled Dart SDK for mobile projects "
        "(installed into the shared sandbox cache)."
    ),
    components=(
        DependencyComponent("Android SDK", "cmdline-tools 11076708, platforms 34–36"),
        DependencyComponent("Flutter", "3.27.4 (bundles Dart)"),
    ),
    container_path="/usr/local/bin/init-mobile-sdk.sh",
)

# Every script offered in Settings → Extra Dependencies. Append to extend.
EXTRA_DEPENDENCY_SCRIPTS: tuple[ExtraDependencyScript, ...] = (MOBILE_SDK_SCRIPT,)


def script_by_key(key: str) -> ExtraDependencyScript | None:
    """Look up a script by its stable ``key`` (used to map widget ids back)."""
    for script in EXTRA_DEPENDENCY_SCRIPTS:
        if script.key == key:
            return script
    return None
