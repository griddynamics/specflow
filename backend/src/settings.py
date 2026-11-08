"""Process settings: one pydantic-settings model per binding, plus the guards.

``DatabaseSettings`` reads ``DATABASE_*`` and ``PGSSL*`` from the process environment. The
other bindings and ``load_settings(role, env)`` follow the environment contract
in docs/configuration.md. The new bindings never read the process
environment on their own: ``load_settings`` hands them the variables, so a
test passes an explicit mapping. Nothing here reads ``.env.local``;
``uv run --env-file ../.env.local`` loads it.
"""

from __future__ import annotations

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
    ValidationInfo,
    field_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from sqlalchemy import URL, make_url
from sqlalchemy.exc import ArgumentError

from cloud_profile import CLOUD_PROVIDERS, CONFIG_FILE_VARIABLE, Cloud, load_profile
from graph.core import LLMConfig
from graph.stores.config import BACKENDS, DEFAULT_BACKEND, StoreConfig
from logger.logger import WARNING, get_logger, log_event
from postgres import SSL_MODES, VERIFY_MODES, ssl_argument

DATABASE_ENVIRONMENT: Final = (
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)

# The libpq TLS names (no DATABASE_ prefix).
DATABASE_TLS_ENVIRONMENT: Final = ("PGSSLMODE", "PGSSLROOTCERT")
# libpq TLS names the app does not support: it builds the TLS context itself, so they are refused, not ignored.
UNSUPPORTED_TLS_ENVIRONMENT: Final = (
    "PGSSLCERT",
    "PGSSLKEY",
    "PGSSLCRL",
    "PGSSLMINPROTOCOLVERSION",
    "PGSSLMAXPROTOCOLVERSION",
)
# The default of PGSSLROOTCERT for verify-ca and verify-full: where the deployment mounts the CA bundle (on
# Aurora, the AWS RDS one).
SSL_ROOT_CERT: Final = "/etc/specflow/postgres/rds-ca-bundle.pem"


def tls_settings(mode: str | None, rootcert: str | None) -> tuple[str | None, str | None]:
    """The ``sslmode`` and the CA bundle it uses: the verify modes default to ``SSL_ROOT_CERT``, ``require``
    uses a bundle only when one is given, the other modes none."""

    if mode in VERIFY_MODES:
        return mode, rootcert or SSL_ROOT_CERT
    return mode, rootcert if mode == "require" else None


def _unsupported_tls(env: Mapping[str, str]) -> list[str]:
    return [name for name in UNSUPPORTED_TLS_ENVIRONMENT if env.get(name, "").strip()]


_DRIVERNAME: Final = "postgresql+asyncpg"

# The documented default of DATABASE_PORT.
DATABASE_PORT: Final = 5432


class DatabaseConfigurationError(ValueError):
    """Raised when the PostgreSQL settings are missing or unusable.

    Messages name variables and rules only, never a value.
    """


class DatabaseSettings(BaseSettings):
    """PostgreSQL connection settings, read from ``DATABASE_*``, ``PGSSLMODE`` and ``PGSSLROOTCERT``.

    ``DATABASE_PORT`` is the port this process connects to (default 5432, applied by
    the loaders); code on the host sets the published host port. Nothing reads
    ``.env.local`` here; ``uv run --env-file ../.env.local`` loads it.
    ``PGSSLMODE`` unset means ``prefer``. A CA bundle the mode uses (``tls_settings``) must be
    readable when the settings load.
    """

    model_config = SettingsConfigDict(env_prefix="DATABASE_", frozen=True)

    host: str
    port: int = Field(ge=1, le=65535)
    name: str
    user: str
    password: SecretStr
    # Blank means unset; the loaders pass blank for an unset name.
    sslmode: str | None = Field(validation_alias="PGSSLMODE")
    sslrootcert: str | None = Field(validation_alias="PGSSLROOTCERT")

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

    @field_validator("sslmode", "sslrootcert", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        return _strip_or_none(value)

    @field_validator("sslmode")
    @classmethod
    def _known_sslmode(cls, value: str | None) -> str | None:
        if value is not None and value not in SSL_MODES:
            raise ValueError(f"must be one of {', '.join(sorted(SSL_MODES))}")
        return value

    @field_validator("sslrootcert")
    @classmethod
    def _readable_rootcert(cls, value: str | None, info: ValidationInfo) -> str | None:
        if "sslmode" in info.data:  # else PGSSLMODE is already refused
            try:
                ssl_argument(*tls_settings(info.data["sslmode"], value))
            except (OSError, ValueError):
                raise ValueError("must be a readable PEM CA bundle") from None
        return value

    @classmethod
    def from_environment(cls, *, tls: bool = True) -> DatabaseSettings:
        """Read the settings from the process environment. ``tls=False`` leaves the TLS names unset, for a
        caller that only needs the URL.

        Raises DatabaseConfigurationError naming every unusable variable.
        """

        if tls and (unsupported := _unsupported_tls(os.environ)):
            raise DatabaseConfigurationError(f"Not supported, unset them: {', '.join(unsupported)}.")
        # No field has a default (a config class): an unset ``DATABASE_PORT`` takes 5432 and an
        # unset TLS name counts as blank, here and in ``_build_database``.
        defaults: dict[str, Any] = {name: "" for name in DATABASE_TLS_ENVIRONMENT if not tls or name not in os.environ}
        if "DATABASE_PORT" not in os.environ:
            defaults["port"] = DATABASE_PORT
        try:
            return cls(**defaults)
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

    def connect_args(self) -> dict[str, Any]:
        """The asyncpg ``ssl`` argument of ``PGSSLMODE`` and ``PGSSLROOTCERT``, explicit for every mode."""

        return {"ssl": ssl_argument(*tls_settings(self.sslmode, self.sslrootcert))}


def _database_variable(location: str | int) -> str:
    """The variable of a ``DatabaseSettings`` error location: a TLS name as it is, else ``DATABASE_<FIELD>``."""

    name = str(location)
    return name if name in DATABASE_TLS_ENVIRONMENT else f"DATABASE_{name.upper()}"


def _describe(error: ValidationError) -> str:
    missing: list[str] = []
    invalid: dict[str, str] = {}
    for item in error.errors(include_url=False, include_input=False):
        variable = _database_variable(item["loc"][0])
        if item["type"] == "missing":
            missing.append(variable)
        else:
            invalid[variable] = item["msg"].removeprefix("Value error, ")
    parts = []
    if missing:
        names = [name for name in DATABASE_ENVIRONMENT if name in missing]
        parts.append(f"Required PostgreSQL variables are not set: {', '.join(names)}.")
    parts.extend(
        f"{name}: {invalid[name]}." for name in (*DATABASE_ENVIRONMENT, *DATABASE_TLS_ENVIRONMENT) if name in invalid
    )
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
TokenType = Literal["none", "keycloak", "rfc9068", "claim"]
DEFAULT_SUBJECT_CLAIM: Final = "sub"
DEFAULT_CLIENT_ID_CLAIMS: Final = ("azp", "client_id")
DEFAULT_EMAIL_CLAIM: Final = "email"
DEFAULT_EMAIL_VERIFIED_CLAIM: Final = "email_verified"
DEFAULT_IGNORED_AUDIENCES: Final = ("account",)
# Claims an ID token carries too: as the ``AUTH_TOKEN_TYPE=claim`` marker they would make the check vacuous.
RESERVED_MARKER_CLAIMS: Final = frozenset(
    {"iss", "sub", "aud", "exp", "nbf", "iat", "jti", "azp", "client_id", "email", "email_verified"}
    | {"acr", "amr", "sid", "auth_time", "nonce", "at_hash", "c_hash", "s_hash"}
)
# The documented defaults of API_PORT and WORKER_HEALTH_PORT.
API_PORT: Final = 8000
WORKER_HEALTH_PORT: Final = 8001
# The MCP role (FR-MCP-OPS-01): default port, the one endpoint path, the default client.
MCP_PORT: Final = 8002
MCP_PATH: Final = "/mcp"
MCP_CLIENT_ID: Final = "specflow-mcp"
PRM_PATH: Final = "/.well-known/oauth-protected-resource"
# ``AUTH_MODE`` is no setting any more; ``AUTH_MODE=dev`` is refused with this hint (AUTH_DEV_SIGNING_KEY is ignored).
AUTH_MODE_REMOVED_HINT: Final = (
    "AUTH_MODE=dev was removed: delete AUTH_MODE and AUTH_DEV_SIGNING_KEY from your env file; "
    "local sign-in uses the bundled Keycloak (QUICKSTART.md)"
)
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
    """Token verification settings: OIDC only."""

    blank_is_unset: ClassVar[tuple[str, ...]] = (
        "AUTH_API_AUDIENCE",
        "AUTH_API_CLIENT_IDS",
        "AUTH_SUBJECT_CLAIM",
        "AUTH_CLIENT_ID_CLAIM",
        "AUTH_EMAIL_CLAIM",
        "AUTH_TOKEN_TYPE",
        "AUTH_TOKEN_TYPE_CLAIM",
        "AUTH_TOKEN_TYPE_VALUE",
        "AUTH_IGNORED_AUDIENCES",
    )

    # Required (and https outside local); see ``_apply_guards`` and ``_guard_deployed_auth``.
    issuer: OptionalText = Field(default=None, validation_alias="OIDC_ISSUER")
    jwks_url: OptionalText = Field(default=None, validation_alias="AUTH_JWKS_URL")
    api_audience: Text = Field(default="specflow-api", validation_alias="AUTH_API_AUDIENCE")
    api_client_ids: Csv = Field(default=("specflow-web",), validation_alias="AUTH_API_CLIENT_IDS")
    allowed_email_domains: Csv = Field(validation_alias="AUTH_ALLOWED_EMAIL_DOMAINS")
    # The claim profile (FR-MCP-AUTH-07). Only ``email_verified_claim`` is not blank-is-unset.
    subject_claim: Text = Field(default=DEFAULT_SUBJECT_CLAIM, validation_alias="AUTH_SUBJECT_CLAIM")
    client_id_claims: Csv = Field(default=DEFAULT_CLIENT_ID_CLAIMS, validation_alias="AUTH_CLIENT_ID_CLAIM")
    email_claim: Text = Field(default=DEFAULT_EMAIL_CLAIM, validation_alias="AUTH_EMAIL_CLAIM")
    email_verified_claim: BlankableText = Field(
        default=DEFAULT_EMAIL_VERIFIED_CLAIM, validation_alias="AUTH_EMAIL_VERIFIED_CLAIM"
    )
    token_type: TokenType = Field(default="none", validation_alias="AUTH_TOKEN_TYPE")
    # The marker claim and value of ``AUTH_TOKEN_TYPE=claim``; both required there, refused elsewhere.
    typ_claim: StrippedOptionalText = Field(default=None, validation_alias="AUTH_TOKEN_TYPE_CLAIM")
    typ_value: StrippedOptionalText = Field(default=None, validation_alias="AUTH_TOKEN_TYPE_VALUE")
    ignored_audiences: Csv = Field(default=DEFAULT_IGNORED_AUDIENCES, validation_alias="AUTH_IGNORED_AUDIENCES")

    @field_validator("api_client_ids", "allowed_email_domains", "client_id_claims")
    @classmethod
    def _at_least_one(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _non_empty(value)

    # The two checks read ``token_type`` through a local name: ruff S105 flags a "token" name compared to a literal.
    @property
    def checks_token_type(self) -> bool:
        """True for every ``AUTH_TOKEN_TYPE`` but ``none``: an access token is told from an ID token."""

        rule = self.token_type
        return rule != "none"

    @property
    def uses_type_claim(self) -> bool:
        """True for ``AUTH_TOKEN_TYPE=claim``: the marker claim and value are required."""

        rule = self.token_type
        return rule == "claim"


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


# GRAPH_STORE_* name -> the NEO4J_* name accepted in its place while GRAPH_STORE is neo4j (one release).
NEO4J_ALIASES: Final = {
    "GRAPH_STORE_URI": "NEO4J_URI",
    "GRAPH_STORE_USER": "NEO4J_USER",
    "GRAPH_STORE_PASSWORD": "NEO4J_PASSWORD",
    "GRAPH_STORE_NAME": "NEO4J_DATABASE",
}
# The documented default of GRAPH_STORE_NAME per backend; the others leave it to the adapter.
GRAPH_STORE_DEFAULT_NAMES: Final = {"neo4j": "neo4j", "postgres": "graph"}
_ALIAS_LOGGER = get_logger("settings")


class GraphStoreSettings(_MappedSettings):
    """The graph store: which backend and how to reach it. The password may be empty locally, never elsewhere.

    Built by ``_build_graph_store`` (aliases, the PostgreSQL ``DATABASE_*`` reuse), never read from the
    environment directly. ``to_store_config`` maps it onto the graph library's ``StoreConfig``.
    """

    blank_is_unset: ClassVar[tuple[str, ...]] = (
        "GRAPH_STORE",
        "GRAPH_STORE_NAME",
        "GRAPH_STORE_ADMIN_URI",
        "GRAPH_STORE_POOL_SIZE",
    )

    backend: Literal["neo4j", "arcadedb", "postgres", "tigergraph"] = Field(
        default=DEFAULT_BACKEND, validation_alias="GRAPH_STORE"
    )
    uri: Text = Field(validation_alias="GRAPH_STORE_URI")
    user: Text = Field(validation_alias="GRAPH_STORE_USER")
    password: SecretStr = Field(validation_alias="GRAPH_STORE_PASSWORD")
    name: OptionalText = Field(default=None, validation_alias="GRAPH_STORE_NAME")
    admin_uri: OptionalText = Field(default=None, validation_alias="GRAPH_STORE_ADMIN_URI")
    pool_size: int | None = Field(default=None, gt=0, validation_alias="GRAPH_STORE_POOL_SIZE")

    def to_store_config(self) -> StoreConfig:
        """The graph library's store settings (``graph.stores.config.StoreConfig``)."""

        return StoreConfig(
            backend=self.backend,
            uri=self.uri,
            user=self.user,
            password=self.password,
            name=self.name,
            admin_uri=self.admin_uri,
            pool_size=self.pool_size,
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
# The documented default of GRAPH_LLM_CONCURRENCY (graph.core.LLMConfig.concurrency).
GRAPH_LLM_CONCURRENCY: Final = 8


class GraphBuildSettings(_MappedSettings):
    """The graph build of the API (``/graph_build``): the LLM gateway, the graph models, the state folder.

    Optional: the API builds it only when a ``GRAPH_BUILD_SWITCHES`` variable is set;
    then every required variable must be usable. ``GRAPH_EMBEDDING_DIM`` and
    ``LLM_GATEWAY_*`` are not switches (the dimension defaults to 1536, and the
    agents share the gateway). The key may be empty locally, never elsewhere.
    ``GRAPH_LLM_CONCURRENCY`` (blank: 8) is the library's ``LLMConfig.concurrency``.
    """

    blank_is_unset: ClassVar[tuple[str, ...]] = ("GRAPH_STATE_DIR", "GRAPH_LLM_CONCURRENCY")

    llm_gateway_url: Text = Field(validation_alias="LLM_GATEWAY_URL")
    llm_gateway_api_key: SecretStr = Field(validation_alias="LLM_GATEWAY_API_KEY")
    main_model: Text = Field(validation_alias="GRAPH_LLM_MODEL")
    small_model: Text = Field(validation_alias="GRAPH_SMALL_MODEL")
    embedding_model: Text = Field(validation_alias="GRAPH_EMBEDDING_MODEL")
    embedding_dim: int = Field(default=GRAPH_EMBEDDING_DIM, gt=0, le=4096, validation_alias="GRAPH_EMBEDDING_DIM")
    state_dir: Text = Field(default=DEFAULT_GRAPH_STATE_DIR, validation_alias="GRAPH_STATE_DIR")
    llm_concurrency: int = Field(default=GRAPH_LLM_CONCURRENCY, ge=1, validation_alias="GRAPH_LLM_CONCURRENCY")

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
            concurrency=self.llm_concurrency,
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
    graph_store: GraphStoreSettings | None = None
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
    # The TLS names have no prefix: always passed (blank = unset), so the process environment stays out.
    kwargs.update({name: env.get(name, "") for name in DATABASE_TLS_ENVIRONMENT})
    for name in _unsupported_tls(env):
        _flag(bad, name)
    try:
        # A prefix no variable carries keeps the process environment out.
        return DatabaseSettings(_env_prefix=_NO_PREFIX, **kwargs)
    except ValidationError as error:
        for item in error.errors(include_url=False, include_input=False):
            _flag(bad, _database_variable(item["loc"][0]))
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
    graph_store: GraphStoreSettings | None = None
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
        found.graph_store = _build_graph_store(source, bad, found.database)
        found.mcp = _build(McpSettings, source, bad)
    if role == "api" and GraphBuildSettings.enabled_in(source):
        found.graph_build = _build(GraphBuildSettings, source, bad)
    if role == "api" or (role == "worker" and "agents" in _worker_queues(source)):
        found.graph_store = _build_graph_store(source, bad, found.database)
        found.agent_run = _build(AgentRunSettings, source, bad)
        if role == "worker":
            found.agent = _build(AgentSettings, source, bad)
            if not source.get("DATABASE_AGENT_PASSWORD", "").strip():
                _flag(bad, "DATABASE_AGENT_PASSWORD")
    return found


def _graph_store_value(name: str, env: Mapping[str, str], backend: str) -> tuple[str | None, str]:
    """The value and the variable it came from: ``name``, else its NEO4J_* alias when the backend is neo4j."""

    if env.get(name, "").strip():
        return env[name], name
    alias = NEO4J_ALIASES[name]
    if backend == "neo4j" and env.get(alias, "").strip():
        return env[alias], alias
    # A blank password is a value (the guard refuses it outside local); keep it when the name is set.
    if name == "GRAPH_STORE_PASSWORD":
        for candidate in (name, alias if backend == "neo4j" else None):
            if candidate is not None and candidate in env:
                return env[candidate], candidate
    return None, name


def _graph_store_variable(name: str, env: Mapping[str, str]) -> str:
    """The variable to name in a refusal about ``name``: the alias when that is where the value came from."""

    backend = env.get("GRAPH_STORE", "").strip() or DEFAULT_BACKEND
    return _graph_store_value(name, env, backend)[1]


def _build_graph_store(
    env: Mapping[str, str], bad: list[str], database: DatabaseSettings | None
) -> GraphStoreSettings | None:
    """Build the graph store binding.

    ``GRAPH_STORE`` (default neo4j) picks the backend. For neo4j each ``NEO4J_*`` name stands in for its
    ``GRAPH_STORE_*`` name when that one is unset (``graph.settings.alias_used`` lists the names, once).
    For postgres an unset ``GRAPH_STORE_URI`` reuses the ``DATABASE_*`` connection (``GRAPH_STORE_NAME`` is the
    schema, default ``graph``); user and password then default to the app role's. Either URI takes the TLS
    settings of ``PGSSLMODE`` and ``PGSSLROOTCERT`` that it does not set itself (``_with_tls``).
    """

    backend = env.get("GRAPH_STORE", "").strip() or DEFAULT_BACKEND
    if backend not in BACKENDS:
        _flag(bad, "GRAPH_STORE")
        return None
    resolved, used = _graph_store_inputs(env, backend)
    if backend == "postgres" and "GRAPH_STORE_URI" not in resolved:
        if database is None:  # DATABASE_* are already flagged
            return None
        uri = URL.create("postgresql", host=database.host, port=database.port, database=database.name)
        resolved["GRAPH_STORE_URI"] = uri.render_as_string()
        resolved.setdefault("GRAPH_STORE_USER", database.user)
        resolved.setdefault("GRAPH_STORE_PASSWORD", database.password.get_secret_value())
    if backend == "postgres" and database is not None:
        resolved["GRAPH_STORE_URI"] = _with_tls(resolved["GRAPH_STORE_URI"], database)
    problems: list[str] = []
    found = _build(GraphStoreSettings, resolved, problems)
    for name in problems:
        # A refusal names the variable the operator set (the alias), else the GRAPH_STORE_* name.
        _flag(bad, _graph_store_value(name, env, backend)[1] if name in NEO4J_ALIASES else name)
    if used:
        log_event(_ALIAS_LOGGER, WARNING, "graph.settings.alias_used", aliases=sorted(used))
    return found


def _with_tls(uri: str, database: DatabaseSettings) -> str:
    """``uri`` with the ``sslmode`` and CA bundle of ``database`` where it sets none of its own, as libpq applies
    ``PGSSLMODE`` and ``PGSSLROOTCERT`` to every connection. A malformed URI is left for the store to refuse."""

    try:
        url = make_url(uri)
    except (ArgumentError, ValueError):
        return uri
    query: dict[str, str] = {}
    if "sslmode" not in url.query and database.sslmode:
        query["sslmode"] = database.sslmode
    mode = url.query.get("sslmode", database.sslmode)
    rootcert = tls_settings(mode, database.sslrootcert)[1] if isinstance(mode, str) else None
    if "sslrootcert" not in url.query and rootcert:
        query["sslrootcert"] = rootcert
    return url.update_query_dict(query).render_as_string(hide_password=False) if query else uri


def _graph_store_inputs(env: Mapping[str, str], backend: str) -> tuple[dict[str, str], list[str]]:
    """The ``GRAPH_STORE_*`` values of ``env`` (aliases and per-backend defaults applied) and the aliases used."""

    resolved: dict[str, str] = {"GRAPH_STORE": backend}
    used: list[str] = []
    for name in NEO4J_ALIASES:
        value, source = _graph_store_value(name, env, backend)
        if source != name:
            used.append(source)
        if value is not None:
            resolved[name] = value
    resolved.update({name: env[name] for name in ("GRAPH_STORE_ADMIN_URI", "GRAPH_STORE_POOL_SIZE") if name in env})
    if "GRAPH_STORE_NAME" not in resolved and backend in GRAPH_STORE_DEFAULT_NAMES:
        resolved["GRAPH_STORE_NAME"] = GRAPH_STORE_DEFAULT_NAMES[backend]
    return resolved, used


def load_storage_settings(env: Mapping[str, str] | None = None) -> StorageSettings:
    """Build only the storage binding (cleanup tooling). Raises ``ConfigurationError`` naming each bad variable."""

    source: Mapping[str, str] = os.environ if env is None else env
    bad: list[str] = []
    found = _build_storage(source, bad)
    if bad or found is None:
        raise ConfigurationError(bad)
    return found


def load_graph_store_settings(env: Mapping[str, str] | None = None) -> GraphStoreSettings:
    """Build only the graph store binding (examples, tooling). Raises ``ConfigurationError`` naming each bad variable.

    Reads ``DATABASE_*`` too, but only a PostgreSQL store without ``GRAPH_STORE_URI`` needs them.
    """

    source: Mapping[str, str] = os.environ if env is None else env
    bad: list[str] = []
    database_bad: list[str] = []
    found = _build_graph_store(source, bad, _build_database(source, database_bad))
    if found is None and not bad:
        bad.extend(database_bad)
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
        raise ConfigurationError(bad, AUTH_MODE_REMOVED_HINT if "AUTH_MODE" in bad else "")
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
        graph_store=found.graph_store,
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
    graph_store, agent, langfuse, graph_build = found.graph_store, found.agent, found.langfuse, found.graph_build
    if (
        role == "worker"
        and database is not None
        and agent_database is not None
        and agent_database.user.lower() == database.user.lower()
    ):
        _flag(bad, "DATABASE_AGENT_USER")
    if langfuse is not None and langfuse.enabled and langfuse.base_url is None:
        _flag(bad, "LANGFUSE_BASE_URL")
    if role in ("api", "mcp"):
        _guard_oidc_env(bad, env)
    if auth is not None:
        _guard_token_type_marker(bad, auth)
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
    _guard_deployed_secrets(bad, graph_store, agent, graph_build, env)


def _guard_oidc_env(bad: list[str], env: Mapping[str, str]) -> None:
    """The api and mcp roles: ``AUTH_MODE=dev`` is refused, and ``OIDC_ISSUER`` is required."""

    if env.get("AUTH_MODE", "").strip().lower() == "dev":
        _flag(bad, "AUTH_MODE")  # removed: load_settings adds the hint
    if not env.get("OIDC_ISSUER", "").strip():
        _flag(bad, "OIDC_ISSUER")


def _guard_deployed_secrets(
    bad: list[str],
    graph_store: GraphStoreSettings | None,
    agent: AgentSettings | None,
    graph_build: GraphBuildSettings | None,
    env: Mapping[str, str],
) -> None:
    """The secrets that must be non-empty when ``APP_ENV`` is not ``local``."""

    if graph_store is not None and not graph_store.password.get_secret_value().strip():
        _flag(bad, _graph_store_variable("GRAPH_STORE_PASSWORD", env))
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


def _guard_token_type_marker(bad: list[str], auth: AuthSettings) -> None:
    """``AUTH_TOKEN_TYPE_CLAIM`` and ``_VALUE``: both under ``claim``, neither under another mode (FR-MCP-AUTH-07)."""

    for name, value in (("AUTH_TOKEN_TYPE_CLAIM", auth.typ_claim), ("AUTH_TOKEN_TYPE_VALUE", auth.typ_value)):
        if (value is not None) != auth.uses_type_claim:
            _flag(bad, name)
    reserved = reserved_marker_claims(
        auth.subject_claim, auth.client_id_claims, auth.email_claim, auth.email_verified_claim
    )
    if auth.typ_claim in reserved:  # ``typ`` stays allowed: ``typ`` = ``Bearer`` is the ``keycloak`` rule
        _flag(bad, "AUTH_TOKEN_TYPE_CLAIM")


def reserved_marker_claims(
    subject_claim: str, client_id_claims: Sequence[str], email_claim: str, email_verified_claim: str
) -> frozenset[str]:
    """The names ``AUTH_TOKEN_TYPE_CLAIM`` must not take: registered and identity claims, and the configured ones."""

    configured = {subject_claim, *client_id_claims, email_claim, email_verified_claim}
    return RESERVED_MARKER_CLAIMS | {name for name in configured if name}


def _guard_api_audience(bad: list[str], auth: AuthSettings) -> None:
    """The ``api`` audience guards, every environment and mode (FR-MCP-AUTH-12).

    An audience equal to a client id would accept an ID token, so it is refused
    unless a token-type check (any mode but ``none``) tells the access token
    apart; an ignored audience equal to the API audience would drop the rule.
    """

    if not auth.checks_token_type and auth.api_audience in auth.api_client_ids:
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

    The audience must not be a client id (an ID token would pass) unless a
    token-type check is set (any mode but ``none``), the REST API audience or
    one of the REST client ids (a REST token would pass), or an ignored
    audience (the rule drops). A client audience also refuses any MCP client
    id that is a REST client id: each surface needs its own client.
    Only the ``mcp`` role has the settings (``mcp`` is ``None`` for the others).
    """

    if mcp is None:
        return
    client_audience = not auth.checks_token_type and mcp.audience in mcp.client_ids
    if client_audience or mcp.audience == auth.api_audience or mcp.audience in auth.api_client_ids:
        _flag(bad, "MCP_AUDIENCE")  # the last: a REST client's token would pass on ``/mcp``
    if mcp.audience in mcp.client_ids and set(mcp.client_ids) & set(auth.api_client_ids):
        _flag(bad, "MCP_CLIENT_IDS")  # a client audience: the surfaces must not share a client
    if {mcp.audience, auth.api_audience} & set(auth.ignored_audiences):
        _flag(bad, "AUTH_IGNORED_AUDIENCES")


def _guard_deployed_auth(bad: list[str], auth: AuthSettings, *, smoke_client: bool) -> None:
    """The auth guards that apply when ``APP_ENV`` is not ``local`` (AC-1.3)."""

    issuer = urlsplit(auth.issuer or "")
    if auth.issuer is not None and (issuer.scheme != "https" or not issuer.hostname):
        _flag(bad, "OIDC_ISSUER")
    if auth.jwks_url is not None:
        jwks = urlsplit(auth.jwks_url)
        if jwks.scheme != "https" or auth.issuer is None or _origin(auth.jwks_url) != _origin(auth.issuer):
            _flag(bad, "AUTH_JWKS_URL")
    if smoke_client and SMOKE_CLIENT_ID in auth.api_client_ids:  # the api role only: mcp reads MCP_CLIENT_IDS
        _flag(bad, "AUTH_API_CLIENT_IDS")
