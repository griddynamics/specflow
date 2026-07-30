"""
Core string enumerations for SpecFlow backend.

Values are byte-for-byte identical to the raw strings previously used in
environment variables and comparison sites so that existing env values and
== comparisons continue to work without change.
"""

from enum import StrEnum


class LLMProvider(StrEnum):
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"


class AuthMode(StrEnum):
    API_KEY = "api_key"
    LOCAL = "local"


class DatabaseType(StrEnum):
    MEMORY = "memory"
    EMULATOR = "emulator"
    FIRESTORE = "firestore"
    SQLITE = "sqlite"


class BackendRuntime(StrEnum):
    """Where/how the backend service is launched — and therefore what provides
    the OS-level isolation boundary around the agents.

    - ``DOCKER`` (default): the backend runs in a container; the container *is*
      the boundary, so no in-process agent sandbox is engaged.
    - ``PROCESS``: the backend runs directly on the host (bare-metal uvicorn);
      the container boundary is gone, so agents must be confined by the OS-level
      Bash sandbox (bubblewrap on Linux, Seatbelt on macOS). See
      ``app/agents_sandboxing/os_sandbox.py``.
    """

    DOCKER = "docker"
    PROCESS = "process"
