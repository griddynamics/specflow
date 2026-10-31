"""Process settings: one pydantic-settings model per binding, plus the guards.

``DatabaseSettings`` reads ``DATABASE_*`` from the process environment. The
other bindings and ``load_settings(role, env)`` follow the environment contract
in docs/configuration.md. The new bindings never read the process
environment on their own: ``load_settings`` hands them the variables, so a
test passes an explicit mapping. Nothing here reads ``.env.local``;
``uv run --env-file ../.env.local`` loads it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTP_PORT, HTTPS_PORT
from typing import Annotated, Any, ClassVar, Final, Literal, override
from urllib.parse import urlsplit

from pydantic import (
    BeforeValidator,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from sqlalchemy import URL

from cloud_profile import CLOUD_PROVIDERS, CONFIG_FILE_VARIABLE, Cloud, load_profile
from graph.core import LLMConfig, Neo4jConfig

DATABASE_ENVIRONMENT: Final = (
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)

_DRIVERNAME: Final = "postgresql+asyncpg"

# The documented default of DATABASE_PORT.
DATABASE_PORT: Final = 5432


class DatabaseConfigurationError(ValueError):
    """Raised when the PostgreSQL settings are missing or unusable.

    Messages name variables and rules only, never a value.
    """


class DatabaseSettings(BaseSettings):
    """PostgreSQL connection settings, read from ``DATABASE_*``.

    ``DATABASE_PORT`` is the port this process connects to (default 5432, applied by
    the loaders); code on the host sets the published host port. Nothing reads
    ``.env.local`` here; ``uv run --env-file ../.env.local`` loads it.
    """

    model_config = SettingsConfigDict(env_prefix="DATABASE_", frozen=True)

    host: str
    port: int = Field(ge=1, le=65535)
    name: str
    user: str
    password: SecretStr

    @field_validator("host", "name", "user")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("password")
    @classmethod
    def _password_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    @classmethod
    def from_environment(cls) -> DatabaseSettings:
        """Read the settings from the process environment.

        Raises DatabaseConfigurationError naming every unusable variable.
        """

        # ``port`` has no field default (a config class): the default is applied here, and
        # in ``_build_database``, when ``DATABASE_PORT`` is unset.
        try:
            if "DATABASE_PORT" in os.environ:
                return cls()
            return cls(port=DATABASE_PORT)
        except ValidationError as error:
            raise DatabaseConfigurationError(_describe(error)) from None

    def url(self) -> URL:
        """Build the SQLAlchemy URL; special characters are escaped on render."""

        return URL.create(
            _DRIVERNAME,
            username=self.user,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.name,
        )


def _describe(error: ValidationError) -> str:
    missing: list[str] = []
    invalid: dict[str, str] = {}
    for item in error.errors(include_url=False, include_input=False):
        variable = f"DATABASE_{str(item['loc'][0]).upper()}"
        if item["type"] == "missing":
            missing.append(variable)
        else:
            invalid[variable] = item["msg"].removeprefix("Value error, ")
    parts = []
    if missing:
        names = [name for name in DATABASE_ENVIRONMENT if name in missing]
        parts.append(f"Required PostgreSQL variables are not set: {', '.join(names)}.")
    parts.extend(f"{name}: {invalid[name]}." for name in DATABASE_ENVIRONMENT if name in invalid)
    parts.append("Copy `.env.example` to `.env`, fill them in, and run with `uv run --env-file ../.env.local`.")
    return " ".join(parts)


# --- Role bindings ---------------------------------------------------------

Role = Literal["api", "worker", "migrate", "mcp"]
ROLES: Final[tuple[str, ...]] = ("api", "worker", "migrate", "mcp")
APP_ENVIRONMENTS: Final = ("local", "dev", "prod")
WORKER_QUEUE_NAMES: Final = ("parse", "agents")
SMOKE_CLIENT_ID: Final = "specflow-smoke"

# Claim profile defaults (FR-MCP-AUTH-07): provider neutral; one source for
# ``AuthSettings`` and ``auth.claims.ClaimPolicy``.
TokenType = Literal["none", "keycloak", "rfc9068"]
DEFAULT_SUBJECT_CLAIM: Final = "sub"
DEFAULT_CLIENT_ID_CLAIMS: Final = ("azp", "client_id")
DEFAULT_EMAIL_CLAIM: Final = "email"
DEFAULT_EMAIL_VERIFIED_CLAIM: Final = "email_verified"
DEFAULT_IGNORED_AUDIENCES: Final = ("account",)
# HS256 signs with SHA-256: a key shorter than its digest weakens the MAC.
DEV_SIGNING_KEY_MIN_BYTES: Final = hashlib.sha256().digest_size
# The documented defaults of API_PORT and WORKER_HEALTH_PORT.
API_PORT: Final = 8000
WORKER_HEALTH_PORT: Final = 8001
# The MCP role (FR-MCP-OPS-01): default port, the one endpoint path, the default client.
MCP_PORT: Final = 8002
MCP_PATH: Final = "/mcp"
MCP_CLIENT_ID: Final = "specflow-mcp"
PRM_PATH: Final = "/.well-known/oauth-protected-resource"
_NO_PREFIX: Final = "__SPECFLOW_NO_ENV__"


class ConfigurationError(ValueError):
    """Raised when the process configuration is missing, invalid or refused.

    ``variables`` names every offending environment variable; the message
    carries names only, never a value.
    """

    def __init__(self, variables: Sequence[str], hint: str = "") -> None:
        self.variables: tuple[str, ...] = tuple(variables)
        self.hint = hint
        message = "Invalid configuration, check these variables: " + ", ".join(self.variables)
        super().__init__(f"{message}. {hint}" if hint else message)

    @override
    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.variables, self.hint))


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _text(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
    return value


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _strip_or_none(value: Any) -> Any:
    """Stripped text, a blank one is unset: guards, policy and PRM then see one value."""

    return _blank_to_none(_strip(value))


def _csv(value: Any) -> Any:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return value


def _non_empty(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        raise ValueError("must not be empty")
    return tuple(dict.fromkeys(value))


Text = Annotated[str, BeforeValidator(_text)]
OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]
StrippedOptionalText = Annotated[str | None, BeforeValidator(_strip_or_none)]
OptionalSecret = Annotated[SecretStr | None, BeforeValidator(_blank_to_none)]
Csv = Annotated[tuple[str, ...], BeforeValidator(_csv)]
# Blank stays "": ``AUTH_EMAIL_VERIFIED_CLAIM`` empty means "no check", not "unset".
BlankableText = Annotated[str, BeforeValidator(_strip)]


class _MappedSettings(BaseSettings):
    """A binding fed by ``load_settings`` only, never by the process environment.

    Every field carries its environment variable name as ``validation_alias``.
    The init source is the only source, so a missing key is reported as
    missing even when the developer's shell has it.
    """

    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    # Variables where a blank value means "not set, use the default".
    blank_is_unset: ClassVar[tuple[str, ...]] = ()

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls, env_settings, dotenv_settings, file_secret_settings  # the init source is the only one
        return (init_settings,)

    @classmethod
    def variables(cls) -> tuple[str, ...]:
        return tuple(str(field.validation_alias) for field in cls.model_fields.values())


class AppSettings(_MappedSettings):
    """``APP_ENV`` (required, no default), ``API_PORT`` and ``WORKER_HEALTH_PORT``."""

    blank_is_unset: ClassVar[tuple[str, ...]] = ("API_PORT", "WORKER_HEALTH_PORT")

    app_env: Literal["local", "dev", "prod"] = Field(validation_alias="APP_ENV")
    api_port: int = Field(default=API_PORT, ge=1, le=65535, validation_alias="API_PORT")
    # Liveness-only HTTP port of a worker process (GET /healthz).
    worker_health_port: int = Field(default=WORKER_HEALTH_PORT, ge=1, le=65535, validation_alias="WORKER_HEALTH_PORT")


class AgentDatabaseSettings(_MappedSettings):
    """The row-level-security role used by agent tool activities.

    The password is absent for ``migrate``, which never receives it.
    """

    blank_is_unset: ClassVar[tuple[str, ...]] = ("DATABASE_AGENT_USER",)

    user: Text = Field(default="specflow_agent", validation_alias="DATABASE_AGENT_USER")
    password: OptionalSecret = Field(default=None, validation_alias="DATABASE_AGENT_PASSWORD")

    def as_database_settings(self, db: DatabaseSettings) -> DatabaseSettings:
        """A copy of ``db`` that connects as the agent role."""

        if self.password is None:
            raise ConfigurationError(("DATABASE_AGENT_PASSWORD",))
        return db.model_copy(update={"user": self.user, "password": self.password})


class AuthSettings(_MappedSettings):
    """Token verification settings; ``AUTH_MODE=dev`` is local only."""

    blank_is_unset: ClassVar[tuple[str, ...]] = (
        "AUTH_MODE",
        "AUTH_API_AUDIENCE",
        "AUTH_API_CLIENT_IDS",
        "AUTH_SUBJECT_CLAIM",
        "AUTH_CLIENT_ID_CLAIM",
        "AUTH_EMAIL_CLAIM",
        "AUTH_TOKEN_TYPE",
        "AUTH_IGNORED_AUDIENCES",
    )

    mode: Literal["oidc", "dev"] = Field(default="oidc", validation_alias="AUTH_MODE")
    # Required (and https outside local) when ``mode`` is ``oidc``; see ``_guard_auth``.
    issuer: OptionalText = Field(default=None, validation_alias="OIDC_ISSUER")
    jwks_url: OptionalText = Field(default=None, validation_alias="AUTH_JWKS_URL")
    api_audience: Text = Field(default="specflow-api", validation_alias="AUTH_API_AUDIENCE")
    api_client_ids: Csv = Field(default=("specflow-web",), validation_alias="AUTH_API_CLIENT_IDS")
    allowed_email_domains: Csv = Field(validation_alias="AUTH_ALLOWED_EMAIL_DOMAINS")
    dev_signing_key: OptionalSecret = Field(default=None, validation_alias="AUTH_DEV_SIGNING_KEY")
    # The claim profile (FR-MCP-AUTH-07). Only ``email_verified_claim`` is not blank-is-unset.
    subject_claim: Text = Field(default=DEFAULT_SUBJECT_CLAIM, validation_alias="AUTH_SUBJECT_CLAIM")
    client_id_claims: Csv = Field(default=DEFAULT_CLIENT_ID_CLAIMS, validation_alias="AUTH_CLIENT_ID_CLAIM")
    email_claim: Text = Field(default=DEFAULT_EMAIL_CLAIM, validation_alias="AUTH_EMAIL_CLAIM")
    email_verified_claim: BlankableText = Field(
        default=DEFAULT_EMAIL_VERIFIED_CLAIM, validation_alias="AUTH_EMAIL_VERIFIED_CLAIM"
    )
    token_type: TokenType = Field(default="none", validation_alias="AUTH_TOKEN_TYPE")
    ignored_audiences: Csv = Field(default=DEFAULT_IGNORED_AUDIENCES, validation_alias="AUTH_IGNORED_AUDIENCES")

    @field_validator("api_client_ids", "allowed_email_domains", "client_id_claims")
    @classmethod
    def _at_least_one(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _non_empty(value)


class StorageSettings(_MappedSettings):
    """Object storage (GCS or S3); credentials come from ADC or the AWS chain through the SDK.

    The cloud profile file (``SPECFLOW_CONFIG_FILE``) supplies defaults for these
    fields; ``_build_storage`` merges it under the environment.
    """

    blank_is_unset: ClassVar[tuple[str, ...]] = (
        "STORAGE_BACKEND",
        "AWS_ENDPOINT_URL_S3",
        "AWS_REGION",
        "STORAGE_S3_ADDRESSING",
    )

    # From the profile file only (no env variable); None = no file.
    cloud: Cloud | None = Field(default=None, validation_alias="cloud")
    backend: Literal["gcs", "s3"] = Field(default="gcs", validation_alias="STORAGE_BACKEND")
    bucket: Text = Field(validation_alias="STORAGE_BUCKET")
    prefix: Text = Field(validation_alias="STORAGE_PREFIX")
    # Optional: unset lets the storage client take the project from its credentials.
    google_cloud_project: OptionalText = Field(default=None, validation_alias="GOOGLE_CLOUD_PROJECT")
    # S3 only. Unset endpoint = the AWS endpoint; unset region = the boto3 chain.
    s3_endpoint_url: OptionalText = Field(default=None, validation_alias="AWS_ENDPOINT_URL_S3")
    s3_region: OptionalText = Field(default=None, validation_alias="AWS_REGION")
    s3_addressing: Literal["auto", "path"] = Field(default="auto", validation_alias="STORAGE_S3_ADDRESSING")

    @classmethod
    @override
    def variables(cls) -> tuple[str, ...]:
        return tuple(name for name in super().variables() if name != "cloud")


class TemporalSettings(_MappedSettings):
    """Temporal connection and task queues."""

    blank_is_unset: ClassVar[tuple[str, ...]] = (
        "TEMPORAL_QUEUE_PARSE",
        "TEMPORAL_QUEUE_AGENTS",
        "WORKER_QUEUES",
    )

    address: Text = Field(validation_alias="TEMPORAL_ADDRESS")
    namespace: Text = Field(validation_alias="TEMPORAL_NAMESPACE")
    api_key: OptionalSecret = Field(default=None, validation_alias="TEMPORAL_API_KEY")
    tls_cert: OptionalSecret = Field(default=None, validation_alias="TEMPORAL_TLS_CERT")
    tls_key: OptionalSecret = Field(default=None, validation_alias="TEMPORAL_TLS_KEY")
    queue_parse: Text = Field(default="specflow-parse", validation_alias="TEMPORAL_QUEUE_PARSE")
    queue_agents: Text = Field(default="specflow-agents", validation_alias="TEMPORAL_QUEUE_AGENTS")
    worker_queues: tuple[Literal["parse", "agents"], ...] = Field(
        default=("parse", "agents"), validation_alias="WORKER_QUEUES"
    )

    # WORKER_QUEUES is lenient: empty entries are dropped and duplicates collapse.
    @field_validator("worker_queues", mode="before")
    @classmethod
    def _split_queues(cls, value: Any) -> Any:
        return _csv(value)

    @field_validator("worker_queues")
    @classmethod
    def _queues_not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _non_empty(value)


class Neo4jSettings(_MappedSettings):
    """Neo4j connection. The password may be empty locally, never elsewhere."""

    blank_is_unset: ClassVar[tuple[str, ...]] = ("NEO4J_DATABASE",)

    uri: Text = Field(validation_alias="NEO4J_URI")
    user: Text = Field(validation_alias="NEO4J_USER")
    password: SecretStr = Field(validation_alias="NEO4J_PASSWORD")
    database: Text = Field(default="neo4j", validation_alias="NEO4J_DATABASE")

    def to_graph_config(self) -> Neo4jConfig:
        """The graph library's connection settings (``graph.core.Neo4jConfig``)."""

        return Neo4jConfig(
            uri=self.uri,
            user=self.user,
            password=self.password,
            database=self.database,
        )


_DNS_NAME: Final = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


def _is_host(netloc: str, host: str) -> bool:
    """A DNS name of letter-digit-hyphen labels (no trailing dot), or a bracketed IP literal."""

    if netloc.startswith("["):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return False
        return True
    return _DNS_NAME.fullmatch(host) is not None


def _resource_url(value: Any) -> Any:
    """``MCP_RESOURCE_URL``: a canonical http(s) URL with the path exactly ``/mcp`` (FR-MCP-AUTH-09).

    Canonical means as a client normalizes it, so the PRM ``resource`` and the default audience agree.
    """

    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text.isascii() or not text.isprintable() or any(c in text for c in ' "\\?#'):
        raise ValueError("must be a plain http(s) URL")
    try:
        parts = urlsplit(text)
        _ = parts.port
        host = parts.hostname
    except ValueError as error:
        raise ValueError("must be a valid URL") from error
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc:
        raise ValueError("must be an http(s) URL with a host and no user information")
    if parts.path != MCP_PATH:
        raise ValueError(f"path must be exactly {MCP_PATH}")
    if text != text.lower() or (parts.scheme, parts.port) in {("https", HTTPS_PORT), ("http", HTTP_PORT)}:
        raise ValueError("must be canonical: lower-case scheme and host, no default port")
    if parts.netloc.endswith(":") or not _is_host(parts.netloc, host):
        raise ValueError("must have a DNS host name or a bracketed IP literal, and no empty port")
    return text


def _exact_entries(value: tuple[str, ...]) -> tuple[str, ...]:
    """Host and origin allowlists match exactly: a wildcard or whitespace entry is refused."""

    if any("*" in item or any(c.isspace() for c in item) for item in value):
        raise ValueError("entries match exactly: no wildcard, no whitespace")
    return tuple(dict.fromkeys(value))


ResourceUrl = Annotated[str, BeforeValidator(_resource_url)]


class McpSettings(_MappedSettings):
    """The MCP role (FR-MCP-OPS-01): the public resource URL, audience, clients and transport allowlists."""

    blank_is_unset: ClassVar[tuple[str, ...]] = ("MCP_AUDIENCE", "MCP_CLIENT_IDS", "MCP_PORT")

    resource_url: ResourceUrl = Field(validation_alias="MCP_RESOURCE_URL")
    audience_override: StrippedOptionalText = Field(default=None, validation_alias="MCP_AUDIENCE")
    client_ids: Csv = Field(default=(MCP_CLIENT_ID,), validation_alias="MCP_CLIENT_IDS")
    allowed_hosts: Csv = Field(default=(), validation_alias="MCP_ALLOWED_HOSTS")
    allowed_origins: Csv = Field(default=(), validation_alias="MCP_ALLOWED_ORIGINS")
    port: int = Field(default=MCP_PORT, ge=1, le=65535, validation_alias="MCP_PORT")

    @field_validator("client_ids")
    @classmethod
    def _at_least_one(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _non_empty(value)

    @field_validator("allowed_hosts", "allowed_origins")
    @classmethod
    def _exact(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _exact_entries(value)

    @property
    def audience(self) -> str:
        """The token audience: ``MCP_AUDIENCE``, else the resource URL itself."""

        return self.audience_override or self.resource_url

    @property
    def prm_url(self) -> str:
        """The protected resource metadata URL (FR-MCP-AUTH-01): the origin plus the path-suffixed well-known path."""

        parts = urlsplit(self.resource_url)
        return f"{parts.scheme}://{parts.netloc}{PRM_PATH}{MCP_PATH}"

    @property
    def transport_hosts(self) -> tuple[str, ...]:
        """Allowed ``Host`` values: the resource ``host[:port]`` as clients send it, then ``MCP_ALLOWED_HOSTS``."""

        parts = urlsplit(self.resource_url)
        host = (parts.hostname or "").lower()
        host = f"[{host}]" if ":" in host else host
        default = {"https": HTTPS_PORT, "http": HTTP_PORT}[parts.scheme]
        resource = host if parts.port in (None, default) else f"{host}:{parts.port}"
        return tuple(dict.fromkeys((resource, *self.allowed_hosts)))


class AgentRunSettings(_MappedSettings):
    """What a summarize run needs; the one reader of ``AGENT_MODEL``."""

    blank_is_unset: ClassVar[tuple[str, ...]] = ("AGENT_TRACE_SENSITIVE_DATA",)

    model: Text = Field(validation_alias="AGENT_MODEL")
    trace_sensitive_data: bool = Field(default=False, validation_alias="AGENT_TRACE_SENSITIVE_DATA")


class AgentSettings(_MappedSettings):
    """LLM gateway (OpenAI-compatible route). The model id is ``AgentRunSettings.model``.

    The key may be empty locally, never elsewhere.
    """

    llm_gateway_url: Text = Field(validation_alias="LLM_GATEWAY_URL")
    llm_gateway_api_key: SecretStr = Field(validation_alias="LLM_GATEWAY_API_KEY")


GRAPH_BUILD_SWITCHES: Final = (
    "GRAPH_LLM_MODEL",
    "GRAPH_SMALL_MODEL",
    "GRAPH_EMBEDDING_MODEL",
)
DEFAULT_GRAPH_STATE_DIR: Final = ".graph-build/state"
# The documented default of GRAPH_EMBEDDING_DIM.
GRAPH_EMBEDDING_DIM: Final = 1536


class GraphBuildSettings(_MappedSettings):
    """The graph build of the API (``/graph_build``): the LLM gateway, the graph models, the state folder.

    Optional: the API builds it only when a ``GRAPH_BUILD_SWITCHES`` variable is set;
    then every required variable must be usable. ``GRAPH_EMBEDDING_DIM`` and
    ``LLM_GATEWAY_*`` are not switches (the dimension defaults to 1536, and the
    agents share the gateway). The key may be empty locally, never elsewhere.
    """

    blank_is_unset: ClassVar[tuple[str, ...]] = ("GRAPH_STATE_DIR",)

    llm_gateway_url: Text = Field(validation_alias="LLM_GATEWAY_URL")
    llm_gateway_api_key: SecretStr = Field(validation_alias="LLM_GATEWAY_API_KEY")
    main_model: Text = Field(validation_alias="GRAPH_LLM_MODEL")
    small_model: Text = Field(validation_alias="GRAPH_SMALL_MODEL")
    embedding_model: Text = Field(validation_alias="GRAPH_EMBEDDING_MODEL")
    embedding_dim: int = Field(default=GRAPH_EMBEDDING_DIM, gt=0, le=4096, validation_alias="GRAPH_EMBEDDING_DIM")
    state_dir: Text = Field(default=DEFAULT_GRAPH_STATE_DIR, validation_alias="GRAPH_STATE_DIR")

    @classmethod
    def enabled_in(cls, env: Mapping[str, str]) -> bool:
        return any(env.get(name, "").strip() for name in GRAPH_BUILD_SWITCHES)

    def to_llm_config(self) -> LLMConfig:
        """The graph library's model settings (``graph.core.LLMConfig``), as ``examples/graph_build.py``."""

        return LLMConfig(
            endpoint=self.llm_gateway_url,
            api_key=self.llm_gateway_api_key,
            main_model=self.main_model,
            small_model=self.small_model,
            embedding_model=self.embedding_model,
            embedding_dim=self.embedding_dim,
        )


class LangfuseSettings(_MappedSettings):
    """Optional tracing: enabled when both keys are non-empty."""

    base_url: OptionalText = Field(default=None, validation_alias="LANGFUSE_BASE_URL")
    public_key: OptionalSecret = Field(default=None, validation_alias="LANGFUSE_PUBLIC_KEY")
    secret_key: OptionalSecret = Field(default=None, validation_alias="LANGFUSE_SECRET_KEY")

    @property
    def enabled(self) -> bool:
        return self.public_key is not None and self.secret_key is not None


@dataclass(frozen=True)
class RoleSettings:
    """The bindings one role needs; the others are ``None``."""

    role: str
    app: AppSettings
    database: DatabaseSettings
    agent_database: AgentDatabaseSettings | None = None
    auth: AuthSettings | None = None
    storage: StorageSettings | None = None
    temporal: TemporalSettings | None = None
    neo4j: Neo4jSettings | None = None
    agent_run: AgentRunSettings | None = None
    agent: AgentSettings | None = None
    langfuse: LangfuseSettings | None = None
    graph_build: GraphBuildSettings | None = None  # api only; None = feature off
    mcp: McpSettings | None = None  # mcp role only


def _flag(bad: list[str], name: str) -> None:
    if name not in bad:
        bad.append(name)


def _build[M: _MappedSettings](cls: type[M], env: Mapping[str, str], bad: list[str], **extra: Any) -> M | None:
    kwargs: dict[str, Any] = dict(extra)
    for name in cls.variables():
        if name not in env:
            continue
        if name in cls.blank_is_unset and not env[name].strip():
            continue
        kwargs[name] = env[name]
    try:
        return cls(**kwargs)
    except ValidationError as error:
        for item in error.errors(include_url=False, include_input=False):
            _flag(bad, str(item["loc"][0]))
        return None


def _build_storage(env: Mapping[str, str], bad: list[str]) -> StorageSettings | None:
    """Build the storage binding: init (none) > env > cloud profile file > defaults."""

    profile, problems = load_profile(env.get(CONFIG_FILE_VARIABLE))
    for name in problems:
        _flag(bad, name)
    if problems:
        return None
    if profile is None:
        return _build(StorageSettings, env, bad)
    return _build(StorageSettings, env, bad, cloud=profile.cloud, **profile.values)


def _build_database(env: Mapping[str, str], bad: list[str]) -> DatabaseSettings | None:
    kwargs: dict[str, Any] = {
        name.removeprefix("DATABASE_").lower(): env[name] for name in DATABASE_ENVIRONMENT if name in env
    }
    kwargs.setdefault("port", DATABASE_PORT)
    try:
        # A prefix no variable carries keeps the process environment out.
        return DatabaseSettings(_env_prefix=_NO_PREFIX, **kwargs)
    except ValidationError as error:
        for item in error.errors(include_url=False, include_input=False):
            _flag(bad, f"DATABASE_{str(item['loc'][0]).upper()}")
        return None


@dataclass
class _Bindings:
    """The bindings ``load_settings`` built; each is ``None`` when the role skips it or it was refused."""

    app: AppSettings | None = None
    database: DatabaseSettings | None = None
    agent_database: AgentDatabaseSettings | None = None
    auth: AuthSettings | None = None
    storage: StorageSettings | None = None
    temporal: TemporalSettings | None = None
    neo4j: Neo4jSettings | None = None
    agent_run: AgentRunSettings | None = None
    agent: AgentSettings | None = None
    langfuse: LangfuseSettings | None = None
    graph_build: GraphBuildSettings | None = None
    mcp: McpSettings | None = None


def _build_bindings(role: str, source: Mapping[str, str], bad: list[str]) -> _Bindings:
    """Build every binding ``role`` needs, flagging each refused variable in ``bad``."""

    found = _Bindings(
        app=_build(AppSettings, source, bad),
        database=_build_database(source, bad),
        agent_database=_build(AgentDatabaseSettings, source, bad),
    )
    if role not in ("migrate", "mcp"):  # the mcp role builds no Temporal, storage or Langfuse binding
        found.temporal = _build(TemporalSettings, source, bad)
        found.langfuse = _build(LangfuseSettings, source, bad)
        found.storage = _build_storage(source, bad)
    if role in ("api", "mcp"):
        found.auth = _build(AuthSettings, source, bad)
    if role == "mcp":
        found.neo4j = _build(Neo4jSettings, source, bad)
        found.mcp = _build(McpSettings, source, bad)
    if role == "api" and GraphBuildSettings.enabled_in(source):
        found.graph_build = _build(GraphBuildSettings, source, bad)
    if role == "api" or (role == "worker" and "agents" in _worker_queues(source)):
        found.neo4j = _build(Neo4jSettings, source, bad)
        found.agent_run = _build(AgentRunSettings, source, bad)
        if role == "worker":
            found.agent = _build(AgentSettings, source, bad)
            if not source.get("DATABASE_AGENT_PASSWORD", "").strip():
                _flag(bad, "DATABASE_AGENT_PASSWORD")
    return found


def load_storage_settings(env: Mapping[str, str] | None = None) -> StorageSettings:
    """Build only the storage binding (cleanup tooling). Raises ``ConfigurationError`` naming each bad variable."""

    source: Mapping[str, str] = os.environ if env is None else env
    bad: list[str] = []
    found = _build_storage(source, bad)
    if bad or found is None:
        raise ConfigurationError(bad)
    return found


def load_settings(
    role: Role,
    env: Mapping[str, str] | None = None,
    *,
    queues: Sequence[str] | None = None,
) -> RoleSettings:
    """Build and guard the settings one role needs.

    ``env`` defaults to the process environment. ``queues`` (the worker's
    ``--queue`` options) replaces ``WORKER_QUEUES``. Raises
    ``ConfigurationError`` naming every missing, invalid or refused variable
    at once.
    """

    if role not in ROLES:
        raise ValueError(f"Unknown role: {role!r}")
    source: Mapping[str, str] = os.environ if env is None else env
    if queues is not None:
        source = {**source, "WORKER_QUEUES": ",".join(queues)}
    bad: list[str] = []
    found = _build_bindings(role, source, bad)
    _apply_guards(role, source, bad, found)
    app, database, agent_database = found.app, found.database, found.agent_database
    if bad or app is None or database is None or agent_database is None:
        raise ConfigurationError(bad)
    if role == "migrate":
        agent_database = agent_database.model_copy(update={"password": None})
    elif role in ("api", "mcp"):
        agent_database = None
    return RoleSettings(
        role=role,
        app=app,
        database=database,
        agent_database=agent_database,
        auth=found.auth,
        storage=found.storage,
        temporal=found.temporal,
        neo4j=found.neo4j,
        agent_run=found.agent_run,
        agent=found.agent,
        langfuse=found.langfuse,
        graph_build=found.graph_build,
        mcp=found.mcp,
    )


def _worker_queues(env: Mapping[str, str]) -> tuple[str, ...]:
    return _csv(env.get("WORKER_QUEUES", "")) or WORKER_QUEUE_NAMES


def _origin(url: str) -> tuple[str, int] | None:
    """Normalised (host, port) of an https/http URL; default ports filled in."""

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port or {"https": HTTPS_PORT, "http": HTTP_PORT}.get(parts.scheme)
    except ValueError:
        return None
    return (host, port) if host and port else None


def _apply_guards(
    role: str,
    env: Mapping[str, str],
    bad: list[str],
    found: _Bindings,
) -> None:
    """Startup guards (AC-1.2 to AC-1.4); each failure adds variable names.

    A guard runs whenever the bindings it reads were built, so one bad
    variable never hides another refusal. An unusable ``APP_ENV`` is already
    reported; the non-local guards then wait for it to be fixed.
    """

    database, agent_database, auth, storage = found.database, found.agent_database, found.auth, found.storage
    neo4j, agent, langfuse, graph_build = found.neo4j, found.agent, found.langfuse, found.graph_build
    if (
        role == "worker"
        and database is not None
        and agent_database is not None
        and agent_database.user.lower() == database.user.lower()
    ):
        _flag(bad, "DATABASE_AGENT_USER")
    if langfuse is not None and langfuse.enabled and langfuse.base_url is None:
        _flag(bad, "LANGFUSE_BASE_URL")
    if auth is not None:
        _guard_auth(env, bad, auth)
        if role == "api":
            _guard_api_audience(bad, auth)
        _guard_mcp_audience(bad, auth, found.mcp)
    _guard_storage(bad, storage)
    app_env = env.get("APP_ENV", "").strip()
    if app_env not in APP_ENVIRONMENTS or app_env == "local":
        return
    if auth is not None:
        _guard_deployed_auth(bad, auth, smoke_client=role == "api")
    _guard_deployed_mcp(bad, found.mcp)
    _guard_deployed_storage(bad, storage)
    if neo4j is not None and not neo4j.password.get_secret_value().strip():
        _flag(bad, "NEO4J_PASSWORD")
    if agent is not None and not agent.llm_gateway_api_key.get_secret_value().strip():
        _flag(bad, "LLM_GATEWAY_API_KEY")
    if graph_build is not None and not graph_build.llm_gateway_api_key.get_secret_value().strip():
        _flag(bad, "LLM_GATEWAY_API_KEY")


def _guard_deployed_storage(bad: list[str], storage: StorageSettings | None) -> None:
    """The storage guard that applies when ``APP_ENV`` is not ``local``: a folder prefix, never ``dev/local/``."""

    if storage is not None and (
        not storage.prefix.endswith("/") or storage.prefix.lstrip("/").casefold().startswith("dev/local/")
    ):
        _flag(bad, "STORAGE_PREFIX")


def _guard_storage(bad: list[str], storage: StorageSettings | None) -> None:
    """The storage guards that hold in every environment (a profile file is in use)."""

    if storage is None or storage.cloud is None:
        return
    if storage.backend not in CLOUD_PROVIDERS[storage.cloud]:
        _flag(bad, "STORAGE_BACKEND")
    if not storage.prefix.endswith("/"):
        _flag(bad, "STORAGE_PREFIX")


def _guard_auth(env: Mapping[str, str], bad: list[str], auth: AuthSettings) -> None:
    """The auth guards that hold in every environment (AC-1.4)."""

    key = auth.dev_signing_key
    if auth.mode == "oidc" and key is not None:
        _flag(bad, "AUTH_DEV_SIGNING_KEY")
    if auth.mode == "oidc" and auth.issuer is None:
        _flag(bad, "OIDC_ISSUER")
    if auth.mode == "dev":
        if key is None or len(key.get_secret_value().encode()) < DEV_SIGNING_KEY_MIN_BYTES:
            _flag(bad, "AUTH_DEV_SIGNING_KEY")
        if env.get("KUBERNETES_SERVICE_HOST", "").strip():
            _flag(bad, "AUTH_MODE")


def _guard_api_audience(bad: list[str], auth: AuthSettings) -> None:
    """The ``api`` audience guards, every environment and mode (FR-MCP-AUTH-12).

    An audience equal to a client id would accept an ID token; an ignored
    audience equal to the API audience would drop the audience rule.
    """

    if auth.api_audience in auth.api_client_ids:
        _flag(bad, "AUTH_API_AUDIENCE")
    if auth.api_audience in auth.ignored_audiences:
        _flag(bad, "AUTH_IGNORED_AUDIENCES")


def _guard_deployed_mcp(bad: list[str], mcp: McpSettings | None) -> None:
    """Outside local: an https resource URL and no smoke client in the MCP client list (FR-MCP-AUTH-09)."""

    if mcp is None:
        return
    if urlsplit(mcp.resource_url).scheme != "https":
        _flag(bad, "MCP_RESOURCE_URL")
    if SMOKE_CLIENT_ID in mcp.client_ids:  # the QA direct-grant client, as AUTH_API_CLIENT_IDS refuses it
        _flag(bad, "MCP_CLIENT_IDS")


def _guard_mcp_audience(bad: list[str], auth: AuthSettings, mcp: McpSettings | None) -> None:
    """The ``mcp`` audience guards, every environment and mode (FR-MCP-AUTH-09).

    The audience must not be a client id (an ID token would pass), the REST API
    audience (a REST token would pass), or an ignored audience (the rule drops).
    Only the ``mcp`` role has the settings (``mcp`` is ``None`` for the others).
    """

    if mcp is None:
        return
    if mcp.audience in mcp.client_ids or mcp.audience == auth.api_audience:
        _flag(bad, "MCP_AUDIENCE")
    if {mcp.audience, auth.api_audience} & set(auth.ignored_audiences):
        _flag(bad, "AUTH_IGNORED_AUDIENCES")


def _guard_deployed_auth(bad: list[str], auth: AuthSettings, *, smoke_client: bool) -> None:
    """The auth guards that apply when ``APP_ENV`` is not ``local`` (AC-1.3)."""

    if auth.mode == "dev":
        _flag(bad, "AUTH_MODE")
    if auth.dev_signing_key is not None:
        _flag(bad, "AUTH_DEV_SIGNING_KEY")
    issuer = urlsplit(auth.issuer or "")
    if auth.issuer is not None and (issuer.scheme != "https" or not issuer.hostname):
        _flag(bad, "OIDC_ISSUER")
    if auth.jwks_url is not None:
        jwks = urlsplit(auth.jwks_url)
        if jwks.scheme != "https" or auth.issuer is None or _origin(auth.jwks_url) != _origin(auth.issuer):
            _flag(bad, "AUTH_JWKS_URL")
    if smoke_client and SMOKE_CLIENT_ID in auth.api_client_ids:  # the api role only: mcp reads MCP_CLIENT_IDS
        _flag(bad, "AUTH_API_CLIENT_IDS")
