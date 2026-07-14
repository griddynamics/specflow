import ast
from dataclasses import dataclass
from datetime import timedelta
from functools import cached_property
import os
from typing import Annotated, FrozenSet, Optional

from pydantic import AliasChoices, Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.enums import AuthMode, BackendRuntime, DatabaseType, LLMProvider

# MCP server ids enabled for agent workflows when listed in MCP_SERVERS_ENABLED (comma-separated).
# User-supplied names outside this set are ignored. See docs/agents/enabled-mcps.md.
MCP_PLAYWRIGHT = "playwright"
MCP_FIGMA = "figma"
MCP_FIGMA_SERVER_KEY = "Figma"  # figma-developer-mcp registers under this server name
ROSETTA_SERVER_KEY = "KnowledgeBase"  # ims-mcp registers under this server name
SUPPORTED_MCPS: FrozenSet[str] = frozenset({MCP_PLAYWRIGHT, MCP_FIGMA})
MCP_SERVERS_ENABLED_ENV = "MCP_SERVERS_ENABLED"
MCP_SERVERS_ENABLED_DEFAULT = MCP_PLAYWRIGHT

WORKSPACE_DEFAULT_BRANCH = "main"
WORKSPACE_DEPLOY_WORKFLOW = "deploy.yml"

# Fixed container-internal path to the SQLite file (DATABASE_TYPE=sqlite). It always lives
# under the /root/.specflow bind mount, so it is NOT a user knob — relocate the db by pointing
# SPECFLOW_HOME_MOUNT_PATH at a different host dir. Only host-side seeding and tests override
# SQLITE_DB_PATH (via env) to address the same bind-mounted file by its real host path.
CONTAINER_SQLITE_DB_PATH = "/root/.specflow/db/specflow.db"

# Single source of truth for the P10Y/Compass endpoint.
P10Y_DEFAULT_BASE_URL = "https://compass.p10y.com"

CLAUDE_CODE_SONNET_4_0 = "claude-sonnet-4-0"
CLAUDE_CODE_SONNET_4_5 = "claude-sonnet-4-5"
CLAUDE_CODE_OPUS_4_5 = "claude-opus-4-5"
CLAUDE_CODE_HAIKU_4_5 = "claude-haiku-4-5"

# OpenRouter model id for the first entry in LLM_LOW CSV; also fallback if CSV parsing fails.
LLM_LOW_DEFAULT_FIRST_MODEL = "anthropic/claude-haiku-4.5"
LLM_MEDIUM_DEFAULT_FIRST_MODEL = "anthropic/claude-sonnet-4.6"

DEFAULT_MODEL = CLAUDE_CODE_HAIKU_4_5

GRACE_PERIOD_FOR_EXPIRED_LEASES = timedelta(hours=48)

# Background job polling intervals (seconds)
STUCK_RUNNING_JOB_INTERVAL_SECONDS = 5 * 60
STUCK_INITIALIZING_JOB_INTERVAL_SECONDS = 5 * 60
STUCK_CLEANING_JOB_INTERVAL_SECONDS = 30 * 60
SCHEDULED_WIPE_JOB_INTERVAL_SECONDS = 60 * 60
# Must stay strictly below the OpenRouterPricingCache TTL (1h) so the scheduled
# refresher renews the catalog before it expires, leaving no stale window in
# which OpenRouter queries fall back to (inflated) SDK pricing.
OPENROUTER_PRICING_REFRESH_INTERVAL_SECONDS = 45 * 60

@dataclass
class EmailConfig:
    username: str
    password: str


def is_key_valid(raw_key: Optional[str]) -> bool:
    """True only when ``raw_key`` is a non-blank string.

    A whitespace-only value counts as unset, so callers can test key presence
    without relying on bare truthiness (``if key`` == ``if bool(key)``, which
    would treat ``"  "`` as set).
    """
    return bool(raw_key and str(raw_key).strip())


class Settings(BaseSettings):
    PROJECT_NAME: str = "SpecFlow Backend"
    API_V1_STR: str = "/api/v1"
    
    # Anthropic Configuration
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_BASE_URL: Optional[str] = None  # Allow override
    
    # OpenRouter Configuration
    OPENROUTER_API_KEY: Optional[str] = None  # OpenRouter-specific key
    OPENROUTER_APP_NAME: Optional[str] = "SpecFlow Backend"  # For analytics
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api"  # API root; chat at /v1/chat/completions
    
    # Workspace Configuration
    # Workspace pool model - workspaces are allocated from pool and cloned to /workspaces/{workspace_id}
    # WORKSPACE_BASE_PATH is the base directory where workspaces are stored (defaults to /workspaces)
    WORKSPACE_BASE_PATH: str = Field(default="/workspaces")  # Base path for workspace pool

    # WORKSPACE_DIR is used ONLY as a fallback for workflows that don't receive explicit workspace_path_override
    # In production, all workflows should receive explicit workspace paths from the pool allocation
    # Setting to None by default to catch any code that incorrectly relies on this global default
    WORKSPACE_DIR: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("WORKSPACE_PATH", "WORKSPACE_DIR"),
    )
    
    # Legacy paths - kept for backward compatibility during migration
    AGENT_BASE_PATH: str = "/agent"  # Deprecated: each workspace now has isolated root

    # Isolated Workspace Model - Standards Configuration
    STANDARDS_SOURCE_PATH: str = "/standards_source"  # Build-time location of standards in Docker image
    STANDARDS_DIR_NAME: str = "standards"  # Directory name for standards in each workspace (becomes ./standards)

    # Artifact Store Configuration
    # Archived generation outputs are stored at ARTIFACTS_BASE_PATH/{generation_id}/
    ARTIFACTS_BASE_PATH: str = Field(default="/workspaces/artifacts")

    # Claude Code temp directory — passed to every agent as CLAUDE_CODE_TMPDIR so that
    # Claude Code writes its internal temp files to the persistent NFS volume instead of
    # ephemeral container storage.  Defaults to {WORKSPACE_BASE_PATH}/claude_code_tmpdir.
    # Resolved via model_validator below when not explicitly set.
    CLAUDE_CODE_TMPDIR_PATH: Optional[str] = None
    # Maximum output tokens for Claude Code agent tool calls.  Claude Code
    # honors CLAUDE_CODE_MAX_OUTPUT_TOKENS in the agent subprocess env.
    # Defaults to 60 000 (provider hard cap is 64 k; stay safely below it).
    CLAUDE_CODE_MAX_OUTPUT_TOKENS: Optional[int] = 60000
    # Non-source artifacts (VCS metadata, dependency caches, build/toolchain output) excluded
    # both when snapshotting a workspace for the archive AND when syncing code between parallel
    # workspaces. Concern-neutral name; legacy env CODE_ARCHIVE_EXCLUDE_PATTERNS still accepted.
    EXCLUDED_ARTIFACT_PATTERNS: list[str] = Field(
        validation_alias=AliasChoices(
            "EXCLUDED_ARTIFACT_PATTERNS", "CODE_ARCHIVE_EXCLUDE_PATTERNS"
        ),
        default=[
        # Version control
        ".git",
        # Python
        ".venv", "venv", "__pycache__", "*.pyc", "*.pyo", ".uv", "*.egg-info",
        # Node / JavaScript
        "node_modules", ".npm", ".yarn", ".pnpm-store",
        # Angular CLI cache
        ".angular",
        # PHP (Composer) / Go (go mod vendor) — same directory name
        "vendor",
        # Java / Maven
        "target", ".m2",
        # Build output (language-agnostic)
        "dist", "build", ".next", ".nuxt", ".output", "out", ".gradle", ".cache",
        # Standards directory copied during workspace prep — not user code
        "standards",
        ],
    )

    # Extra patterns ADDED to (never replacing) EXCLUDED_ARTIFACT_PATTERNS when syncing code
    # between parallel workspaces. Primarily a quickstart knob. A Python list, e.g.
    # ['.log', '.data']; blank/unset -> [].
    WORKSPACE_EXCLUDE_PATTERNS: Annotated[list[str], NoDecode] = []

    @field_validator("WORKSPACE_EXCLUDE_PATTERNS", mode="before")
    @classmethod
    def _parse_workspace_exclude_patterns(cls, v: object) -> object:
        """Blank/None -> []; otherwise a Python list literal (single or double quotes), parsed
        with ast.literal_eval (literals only — never eval, so no arbitrary code execution)."""
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return list(v)
        if not isinstance(v, str):
            return v
        s = v.strip()
        if not s:
            return []
        try:
            parsed = ast.literal_eval(s)
        except (ValueError, SyntaxError) as exc:
            raise ValueError(
                "WORKSPACE_EXCLUDE_PATTERNS must be a Python list, e.g. ['.log', '.data']"
            ) from exc
        if not isinstance(parsed, (list, tuple)):
            raise ValueError(
                "WORKSPACE_EXCLUDE_PATTERNS must be a list, e.g. ['.log', '.data']"
            )
        return [str(item) for item in parsed]

    # Agent Logs Configuration
    # Logs are stored in dedicated mount at /agent_logs
    AGENT_LOGS_BASE_PATH: str = Field(default="/agent_logs")

    # Graceful-shutdown / boot recovery
    # Auto-retry sessions interrupted by a server shutdown (SIGTERM / pod eviction)
    # when the server boots back up.
    AUTO_RECOVER_INTERRUPTED_SESSIONS: bool = True
    # Only auto-retry interrupted sessions whose failed_at is within this window.
    SHUTDOWN_RECOVERY_WINDOW_MINUTES: int = 30

    # Notifications Configuration
    ## Slack
    SLACK_WEBHOOK_URL: Optional[str] = None
    ## Emails - sender
    NOTIFY_EMAIL_USERNAME: Optional[str] = None
    NOTIFY_EMAIL_PASSWORD: Optional[str] = None

    # P10Y Configuration
    P10Y_BASE_URL: Optional[str] = P10Y_DEFAULT_BASE_URL
    P10Y_API_KEY: Optional[str] = None
    P10Y_ORGANISATION_ID: Optional[int] = None
    P10Y_REPOSITORY_ID: Optional[int] = None

    @field_validator("P10Y_ORGANISATION_ID", "P10Y_REPOSITORY_ID", mode="before")
    @classmethod
    def _empty_str_to_none_int(cls, v: object) -> object:
        if v == "":
            return None
        return v

    # Git / GitHub — default pool credentials (in-memory after load; see github_platform_secrets)
    # Legacy env names GITHUB_TOKEN / GIT_USER_NAME still accepted via aliases.
    TOKEN_ENCRYPTION_KEY: Optional[str] = Field(
        default=None,
        description="Fernet key (url-safe base64) for encrypting per-API-key GitHub tokens at rest",
    )
    GITHUB_TOKEN_DEFAULT: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_TOKEN_DEFAULT", "GITHUB_TOKEN"),
    )

    # For cloning and https auth
    GIT_USER_NAME_DEFAULT: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GIT_USER_NAME_DEFAULT", "GIT_USER_NAME"),
    )

    # For commit metadata
    GIT_COMMITTER_USER_NAME: str = Field(
        default="SpecFlow System",
        description="Git committer name used for system commits (workspace resets, archives)",
    )
    GIT_COMMITTER_USER_EMAIL: str = Field(
        default="specflow@system.local",
        description="Git committer email used for system commits (workspace resets, archives)",
    )

    # Kubernetes API loading for platform secrets (production). Map arbitrary Secret data keys.
    K8S_SECRET_NAMESPACE: str = ""
    K8S_SECRET_NAME: str = ""
    K8S_SECRET_KEY_ENCRYPTION: str = "token-encryption-key"
    K8S_SECRET_KEY_GITHUB_DEFAULT: str = "github-token-default"
    K8S_SECRET_KEY_GIT_USER_DEFAULT: str = "git-user-default"

    # Database Configuration
    DATABASE_TYPE: DatabaseType = DatabaseType.MEMORY
    FIRESTORE_EMULATOR_HOST: Optional[str] = None  # e.g., localhost:8080 or firestore-emulator:8080
    GCP_PROJECT_ID: Optional[str] = None  # GCP project ID for Firestore
    FIRESTORE_DATABASE_NAME: str = "default"  # Firestore database name (default: "(default)")
    SQLITE_DB_PATH: str = CONTAINER_SQLITE_DB_PATH  # container default; host seeding/tests override via env

    # LLM Provider Configuration
    # DEFAULT_PROVIDER is derived from the API keys below — see the computed_field.

    # LLM Model Tier Configuration
    # Values follow OpenRouter naming convention: provider/model (e.g., anthropic/claude-opus-4.5)
    # Can be comma-separated for multiple models (used in multi-workspace generation for variance reduction)
    # Set LLM_HIGH / LLM_MEDIUM / LLM_LOW env vars to override defaults.
    LLM_HIGH: str = "anthropic/claude-opus-4.8"
    LLM_MEDIUM: str = "anthropic/claude-sonnet-4.6,openai/gpt-5.5,z-ai/glm-5.2"
    LLM_LOW: str = LLM_LOW_DEFAULT_FIRST_MODEL

    # Workspace Count Configuration
    # Number of parallel workspaces per generation run (1, 2, or 3).
    # User can override per-call via MCP tool; this env var sets the global default.
    WORKSPACE_COUNT: int = 3

    # Optional MCP servers for Claude Code agents (comma-separated: playwright, figma).
    # MCP client may override per sync/run; values are filtered to SUPPORTED_MCPS.
    MCP_SERVERS_ENABLED: str = MCP_SERVERS_ENABLED_DEFAULT

    # After spec indexing, prune optional agent MCPs (playwright, figma) using spec keyword grep.
    MCP_AUTO_PRUNE_ENABLED: bool = True
    # If True (default), run a medium LLM on grep evidence only to refine enabled MCPs; if False, keyword hits alone decide.
    MCP_PRUNE_USE_LLM: bool = True
    # Comma-separated case-insensitive substrings scanned in specification_index.md + spec tree.
    MCP_PRUNE_KEYWORDS_FIGMA: str = (
        "figma,figma.com,figma file,framelink,figma design,figma link"
    )
    MCP_PRUNE_KEYWORDS_PLAYWRIGHT: str = (
        "frontend,front-end,ui,ux,browser,web app,webapp,web application,react,angular,vue,svelte,"
        "next.js,nextjs,nuxt,typescript,javascript,.tsx,.jsx,spa,single-page,html,css,dashboard,"
        "playwright mcp,playwright-mcp,playwrightmcp"
    )
    MCP_PRUNE_GREP_MAX_LINES_TOTAL: int = 400
    MCP_PRUNE_GREP_MAX_LINES_PER_MCP: int = 80
    MCP_PRUNE_GREP_MAX_CHARS: int = 48_000

    # Playwright MCP (@playwright/mcp) — browser automation for coding and deploy/QA phases.
    PLAYWRIGHT_MCP_COMMAND: str = "npx"
    PLAYWRIGHT_MCP_ARGS: str = "-y @playwright/mcp@latest"

    # Figma MCP (figma-developer-mcp) — design context; requires token in backend env.
    FIGMA_MCP_COMMAND: str = "npx"
    FIGMA_MCP_ARGS: str = "-y figma-developer-mcp --stdio"
    # Personal access token for Figma API (also accepts FIGMA_API_KEY for compatibility).
    FIGMA_ACCESS_TOKEN: Optional[str] = None
    FIGMA_API_KEY: Optional[str] = None

    # KnowledgeBase/Rosetta MCP — matches `claude mcp add ... -- uvx ims-mcp@latest` env surface.
    ROSETTA_MCP_COMMAND: str = "uvx"
    ROSETTA_MCP_ARGS: str = "ims-mcp@latest"
    ROSETTA_SERVER_URL: Optional[str] = "https://ims.evergreen.gcp.griddynamics.net/"
    ROSETTA_API_KEY: Optional[str] = None
    ROSETTA_USER_EMAIL: Optional[str] = None
    # Passed to ims-mcp subprocess as env VERSION (e.g. r2).
    ROSETTA_IMS_VERSION: str = "r2"
    # Plugin mode is the DEFAULT for every environment (quickstart AND hosted): KB init
    # runs against the bundled Rosetta plugin, no ims-mcp service / ROSETTA_API_KEY needed.
    # Set ROSETTA_MCP_ENABLED=true only to opt back into the live ims-mcp server (it then
    # wins over the plugin). See app/core/mcp_selection.py:for_kb_init.
    ROSETTA_MCP_ENABLED: bool = False
    ROSETTA_OUTPUT_DIR: str = "rosetta"
    # Path to the Rosetta plugin bundled into the image at build time (backend/Dockerfile
    # stages it here). When ROSETTA_MCP_ENABLED is False, WorkspaceManager.provision_rosetta_plugin
    # copies this plugin's agents/skills/commands into each workspace's .claude/ and merges its
    # hooks into .claude/settings.json so setting_sources=["project"] discovers them (no
    # ~/.claude, no SDK plugins= loader). This same path is also exported as CLAUDE_PLUGIN_ROOT
    # per agent (claude_code.setup_rosetta_plugin_env) so the merged hooks' ${CLAUDE_PLUGIN_ROOT}
    # resolves to the read-only image plugin. Set to None / empty to disable plugin provisioning
    # (KB init then no-ops unless MCP is enabled).
    ROSETTA_PLUGIN_PATH: Optional[str] = "/opt/rosetta-plugin"

    # PostHog Telemetry Configuration
    POSTHOG_API_KEY: Optional[str] = None
    POSTHOG_HOST: str = "https://eu.i.posthog.com"  # Default PostHog cloud
    POSTHOG_ENABLED: bool = True  # Feature flag

    # Langfuse LLM Tracing Configuration
    # Tracing is enabled iff all three of PUBLIC_KEY, SECRET_KEY, and BASE_URL are set
    # (see `langfuse_enabled` property below). No separate ENABLED flag to misconfigure.
    LANGFUSE_PUBLIC_KEY: Optional[str] = None
    LANGFUSE_SECRET_KEY: Optional[str] = None
    LANGFUSE_BASE_URL: Optional[str] = None
    LANGFUSE_ENVIRONMENT: Optional[str] = None
    LANGFUSE_REDACT_TOOL_IO: bool = False

    # Quickstart / local-mode settings
    AUTH_MODE: AuthMode = AuthMode.API_KEY
    LOCAL_USER_EMAIL: Optional[str] = None
    LOCAL_USER_NAME: Optional[str] = None
    GITHUB_ORG: Optional[str] = None
    GITHUB_TEAM_SLUG: Optional[str] = None
    WORKSPACE_REPO_PREFIX: str = "specflow-workspace"

    # Backend runtime / agent OS-sandbox settings.
    # DOCKER (default): the container is the isolation boundary; no in-process
    # agent sandbox is engaged (decoupled — Docker behaviour is unchanged).
    # PROCESS: the backend runs bare-metal, so agents are confined by the OS-level
    # Bash sandbox. See app/agents_sandboxing/os_sandbox.py.
    BACKEND_RUNTIME: BackendRuntime = BackendRuntime.DOCKER
    # Optional comma-separated override for the agent sandbox network allowlist
    # (see os_sandbox.DEFAULT_AGENT_SANDBOX_ALLOWED_DOMAINS). Empty → use default.
    AGENT_SANDBOX_ALLOWED_DOMAINS: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @field_validator("DATABASE_TYPE", mode="before")
    @classmethod
    def _validate_database_type(cls, v: object) -> object:
        if isinstance(v, str):
            allowed = {member.value for member in DatabaseType}
            if v not in allowed:
                raise ValueError(
                    f"Invalid DATABASE_TYPE: {v!r}. Allowed values: {sorted(allowed)}"
                )
        return v

    @field_validator("BACKEND_RUNTIME", mode="before")
    @classmethod
    def _validate_backend_runtime(cls, v: object) -> object:
        if isinstance(v, str):
            allowed = {member.value for member in BackendRuntime}
            if v not in allowed:
                raise ValueError(
                    f"Invalid BACKEND_RUNTIME: {v!r}. Allowed values: {sorted(allowed)}"
                )
        return v

    @model_validator(mode="after")
    def _reject_local_auth_in_protected_environments(self) -> "Settings":
        if self.AUTH_MODE != AuthMode.LOCAL:
            return self
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            raise ValueError("AUTH_MODE=local is not allowed in Kubernetes")
        if self.DATABASE_TYPE == DatabaseType.FIRESTORE:
            raise ValueError("AUTH_MODE=local is not allowed with DATABASE_TYPE=firestore")
        return self

    @model_validator(mode="after")
    def _derive_claude_code_tmpdir(self) -> "Settings":
        if self.CLAUDE_CODE_TMPDIR_PATH is None:
            object.__setattr__(
                self,
                "CLAUDE_CODE_TMPDIR_PATH",
                os.path.join(self.WORKSPACE_BASE_PATH, "claude_code_tmpdir"),
            )
        return self

    @computed_field
    @cached_property
    def DEFAULT_PROVIDER(self) -> LLMProvider:
        # Derived solely from which key is present — there is no configurable
        # knob (see .env.quickstart.example: "set ONE of the two keys"), so a
        # provider/key mismatch is impossible by construction: OpenRouter when
        # its key is set (also the documented default when both are), Anthropic
        # when only that key is set. Neither set → OpenRouter, and startup
        # validation then fails fast naming both keys. is_key_valid ignores
        # blank/whitespace-only values.
        if is_key_valid(self.OPENROUTER_API_KEY):
            return LLMProvider.OPENROUTER
        if is_key_valid(self.ANTHROPIC_API_KEY):
            return LLMProvider.ANTHROPIC
        return LLMProvider.OPENROUTER

    @property
    def langfuse_enabled(self) -> bool:
        return bool(
            self.LANGFUSE_PUBLIC_KEY
            and self.LANGFUSE_SECRET_KEY
            and self.LANGFUSE_BASE_URL
        )

    def get_email_config(self) -> Optional[EmailConfig]:
        return EmailConfig(
            username=self.NOTIFY_EMAIL_USERNAME,
            password=self.NOTIFY_EMAIL_PASSWORD,
        ) if all([self.NOTIFY_EMAIL_USERNAME, self.NOTIFY_EMAIL_PASSWORD]) else None

settings = Settings()

GIT_COMMITTER_USER_NAME = settings.GIT_COMMITTER_USER_NAME
GIT_COMMITTER_USER_EMAIL = settings.GIT_COMMITTER_USER_EMAIL

