"""Verified TLS to PostgreSQL (``PGSSLMODE``, ``PGSSLROOTCERT``), offline.

The settings refuse an unusable CA bundle at start, and every engine path (``db.session.build_engine``,
the migrations, the readiness probe, the PostgreSQL graph store) hands asyncpg the same TLS context. The
handshakes run against a loopback server that answers asyncpg's SSLRequest, completes TLS with a test
certificate, accepts the login and then refuses the session (SQLSTATE 3D000): that refusal proves the TLS
handshake worked. (A refusal before the login would make asyncpg's default mode retry without TLS.)
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import shutil
import ssl
import struct
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy import make_url
from sqlalchemy.exc import DBAPIError

import health
import postgres
from db import migrate
from db.session import build_engine
from graph.core.errors import ConfigurationError as StoreConfigurationError
from graph.stores.config import StoreConfig
from graph.stores.postgres.executor import build_engine as build_graph_engine
from graph.stores.postgres.executor import connection_url
from mcp_support import mcp_env
from settings import (
    AppSettings,
    ConfigurationError,
    DatabaseConfigurationError,
    DatabaseSettings,
    Role,
    RoleSettings,
    load_graph_store_settings,
    load_settings,
    tls_settings,
)

# Written out literally: the mount path is the deployment contract.
MOUNTED_CA = "/etc/specflow/postgres/rds-ca-bundle.pem"
SSL_REQUEST = struct.pack("!ii", 8, 80877103)
REFUSED = "3D000"
DB_CRED = "db-pass"
ROOT = Path(__file__).resolve().parents[2]


# A developer's shell must not decide a test: the shared scrub removes every PGSSL* name too.
pytestmark = pytest.mark.usefixtures("clean_specflow_env")


# Test PKI


@dataclass(frozen=True)
class Pki:
    ca: Path
    other_ca: Path
    server_cert: Path
    server_key: Path


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    subject: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: tuple[x509.Certificate, ec.EllipticCurvePrivateKey] | None = None,
    names: tuple[x509.GeneralName, ...] = (),
) -> x509.Certificate:
    now = dt.datetime.now(dt.UTC)
    issuer_cert, issuer_key = issuer if issuer is not None else (None, key)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(issuer_cert.subject if issuer_cert is not None else _name(subject))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    if issuer is None:  # a CA
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        usage = {"key_cert_sign": True, "crl_sign": True, "digital_signature": False}
    else:
        builder = builder.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        builder = builder.add_extension(x509.SubjectAlternativeName(list(names)), critical=False)
        usage = {"key_cert_sign": False, "crl_sign": False, "digital_signature": True}
    flags = dict.fromkeys(
        ("content_commitment", "key_encipherment", "data_encipherment", "key_agreement", "encipher_only"),
        False,
    )
    builder = builder.add_extension(x509.KeyUsage(**usage, **flags, decipher_only=False), critical=True)
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    """A CA, a server certificate it signed for ``localhost`` only, and an unrelated CA."""

    folder = tmp_path_factory.mktemp("pki")
    ca_key, other_key, server_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    ca = _certificate("specflow test CA", ca_key)
    server = _certificate("localhost", server_key, (ca, ca_key), (x509.DNSName("localhost"),))
    paths = Pki(folder / "ca.pem", folder / "other-ca.pem", folder / "server.pem", folder / "server.key")
    paths.ca.write_bytes(_pem(ca))
    paths.other_ca.write_bytes(_pem(_certificate("unrelated CA", other_key)))
    paths.server_cert.write_bytes(_pem(server))
    paths.server_key.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return paths


# Loopback server


@dataclass
class FakePostgres:
    port: int
    seen: list[str] = field(default_factory=list)  # per connection: "tls", "rejected" or "plain"
    alpn: list[str | None] = field(default_factory=list)  # per TLS connection: the protocol the client offered


def _refusal() -> bytes:
    """AuthenticationOk, then an ErrorResponse with the ``REFUSED`` SQLSTATE."""

    body = b"SFATAL\0C" + REFUSED.encode() + b"\0Mfake server\0\0"
    return b"R" + struct.pack("!ii", 8, 0) + b"E" + struct.pack("!i", len(body) + 4) + body


@contextlib.contextmanager
def fake_postgres(pki: Pki) -> Iterator[FakePostgres]:
    """A server on its own thread and loop, so that sync callers (Alembic runs ``asyncio.run``) work too."""

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(pki.server_cert, pki.server_key)
    context.set_alpn_protocols(["postgresql"])
    loop = asyncio.new_event_loop()
    found = FakePostgres(port=0)
    ready = threading.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await reader.readexactly(8)
            if first == SSL_REQUEST:
                writer.write(b"S")
                await writer.drain()
                try:
                    await writer.start_tls(context)
                except (ssl.SSLError, OSError):  # the client refused the server certificate
                    found.seen.append("rejected")
                    return
                found.seen.append("tls")
                found.alpn.append(writer.get_extra_info("ssl_object").selected_alpn_protocol())
                first = await reader.readexactly(8)
            else:
                found.seen.append("plain")
            await reader.readexactly(struct.unpack("!i", first[:4])[0] - 8)  # the rest of the StartupMessage
            writer.write(_refusal())
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def run() -> None:
        asyncio.set_event_loop(loop)
        server = loop.run_until_complete(asyncio.start_server(handle, "127.0.0.1", 0))
        found.port = server.sockets[0].getsockname()[1]
        ready.set()
        loop.run_forever()
        server.close()
        pending = asyncio.all_tasks(loop)
        for task in pending:  # a handshake the client dropped may still wait for bytes
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    try:
        yield found
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)


def database(port: int, host: str = "localhost", *, mode: str = "", rootcert: str = "") -> DatabaseSettings:
    return DatabaseSettings(
        host=host, port=port, name="specflow", user="specflow", password=DB_CRED, PGSSLMODE=mode, PGSSLROOTCERT=rootcert
    )


async def connect(settings: DatabaseSettings) -> BaseException:
    """The error of one connection through the application engine (the loopback server refuses each)."""

    engine = build_engine(settings)
    try:
        async with engine.connect():
            pass
    except (DBAPIError, OSError) as error:
        return error
    finally:
        await engine.dispose()
    raise AssertionError("the loopback server never accepts a session")


def refused(error: BaseException) -> bool:
    return isinstance(error, DBAPIError) and getattr(error.orig, "sqlstate", None) == REFUSED


def certificate_refused(error: BaseException) -> bool:
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
    return False


# The asyncpg ``ssl`` argument


def test_verify_full_trusts_the_bundle_only_and_checks_the_host_name(pki: Pki) -> None:
    context = postgres.ssl_argument("verify-full", str(pki.ca))

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname is True
    assert [ca["subject"] for ca in context.get_ca_certs()] == [((("commonName", "specflow test CA"),),)]


def test_verify_ca_checks_the_chain_but_not_the_host_name(pki: Pki) -> None:
    context = postgres.ssl_argument("verify-ca", str(pki.ca))

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname is False


def test_every_mode_gets_an_explicit_argument(pki: Pki) -> None:
    require = postgres.ssl_argument("require")
    pinned = postgres.ssl_argument("require", str(pki.ca))

    assert postgres.ssl_argument(None) == "prefer" and postgres.ssl_argument("allow") == "allow"
    assert postgres.ssl_argument("disable") is False
    assert isinstance(require, ssl.SSLContext) and require.verify_mode is ssl.CERT_NONE and not require.check_hostname
    # libpq: require with a CA bundle checks the chain (as verify-ca does), not the host name.
    assert isinstance(pinned, ssl.SSLContext) and pinned.verify_mode is ssl.CERT_REQUIRED and not pinned.check_hostname


@pytest.mark.parametrize(
    ("mode", "rootcert"),
    [("verify-full", None), ("verify-full", ""), ("verify-ca", None), ("prefer", "ca"), ("disable", "ca"), ("x", None)],
)
def test_a_mode_without_its_bundle_or_with_a_bundle_it_ignores_is_refused(
    pki: Pki, mode: str, rootcert: str | None
) -> None:
    # An empty bundle never falls back to the system store.
    with pytest.raises(ValueError, match="sslmode"):
        postgres.ssl_argument(mode, str(pki.ca) if rootcert == "ca" else rootcert)


@pytest.mark.parametrize(
    ("name", "error"), [("missing.pem", FileNotFoundError), ("bad.pem", ssl.SSLError), ("", IsADirectoryError)]
)
def test_a_missing_or_non_pem_bundle_raises(tmp_path: Path, name: str, error: type[OSError]) -> None:
    (tmp_path / "bad.pem").write_text("not a certificate\n")

    with pytest.raises(error):
        postgres.ssl_argument("verify-full", str(tmp_path / name))


# Settings


def test_without_pgsslmode_the_connection_is_prefer() -> None:
    settings = database(5432)

    assert (settings.sslmode, settings.sslrootcert) == (None, None)
    assert settings.connect_args() == {"ssl": "prefer"}


def test_the_ca_bundle_defaults_to_the_mounted_path_for_the_verify_modes_only() -> None:
    assert tls_settings("verify-full", None) == ("verify-full", MOUNTED_CA)
    assert tls_settings("require", None) == ("require", None)
    assert tls_settings("prefer", "/ca.pem") == ("prefer", None)
    # verify-full reads the mount at start; no test machine has it.
    with pytest.raises(ValueError, match="PGSSLROOTCERT"):
        database(5432, mode="verify-full")


@pytest.mark.parametrize("role", ["api", "worker", "migrate", "mcp"])
def test_every_role_refuses_to_start_without_a_readable_ca_bundle(
    role: Role, valid_env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "empty.pem").write_text("")
    env = {**mcp_env(valid_env), "PGSSLMODE": "verify-full"}

    for rootcert in ("", str(tmp_path / "missing.pem"), str(tmp_path / "empty.pem")):
        with pytest.raises(ConfigurationError) as raised:
            load_settings(role, {**env, "PGSSLROOTCERT": rootcert})
        assert raised.value.variables == ("PGSSLROOTCERT",)
    # require checks a bundle it is given, and needs none.
    with pytest.raises(ConfigurationError) as raised:
        load_settings(role, {**env, "PGSSLMODE": "require", "PGSSLROOTCERT": str(tmp_path / "missing.pem")})
    assert raised.value.variables == ("PGSSLROOTCERT",)
    assert load_settings(role, {**env, "PGSSLMODE": "require"}).database.sslrootcert is None


@pytest.mark.parametrize("name", ["PGSSLCERT", "PGSSLKEY", "PGSSLCRL", "PGSSLMINPROTOCOLVERSION"])
def test_libpq_tls_names_the_app_does_not_support_are_refused(
    name: str, valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings("migrate", {**valid_env, name: "x"})
    assert raised.value.variables == (name,)

    for variable in ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER"):
        monkeypatch.setenv(variable, "d")
    monkeypatch.setenv("DATABASE_PASSWORD", DB_CRED)
    monkeypatch.setenv(name, "x")
    with pytest.raises(DatabaseConfigurationError, match=name):
        DatabaseSettings.from_environment()


def test_the_environment_loader_names_the_unreadable_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    # The migrations' own loader (``alembic upgrade`` without ``specflow migrate``).
    for name, value in (("DATABASE_HOST", "h"), ("DATABASE_NAME", "d"), ("DATABASE_USER", "u")):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_PASSWORD", DB_CRED)
    monkeypatch.setenv("PGSSLMODE", "verify-full")

    with pytest.raises(DatabaseConfigurationError, match=r"^PGSSLROOTCERT: must be a readable PEM CA bundle"):
        DatabaseSettings.from_environment()
    # The URL alone needs no TLS settings (the offline migrations).
    assert DatabaseSettings.from_environment(tls=False).sslmode is None
    monkeypatch.delenv("PGSSLMODE")
    assert DatabaseSettings.from_environment().connect_args() == {"ssl": "prefer"}


def test_an_unknown_mode_is_refused_and_a_blank_one_is_unset(valid_env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings("migrate", {**valid_env, "PGSSLMODE": "verify"})
    assert raised.value.variables == ("PGSSLMODE",)

    assert load_settings("migrate", {**valid_env, "PGSSLMODE": " "}).database.connect_args() == {"ssl": "prefer"}


def test_the_process_environment_never_reaches_an_explicit_mapping(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGSSLMODE", "verify-full")

    assert load_settings("migrate", valid_env).database.sslmode is None


def test_the_agent_role_and_the_graph_store_reuse_the_tls_settings(valid_env: dict[str, str], pki: Pki) -> None:
    env = {**valid_env, "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(pki.ca), "GRAPH_STORE": "postgres"}
    for name in ("GRAPH_STORE_URI", "GRAPH_STORE_USER", "GRAPH_STORE_PASSWORD"):
        env.pop(name)

    worker = load_settings("worker", env)
    assert worker.agent_database is not None
    agent = worker.agent_database.as_database_settings(worker.database)
    assert (agent.sslmode, agent.sslrootcert) == ("verify-full", str(pki.ca))

    store = load_graph_store_settings(env)
    _, args = connection_url(StoreConfig(backend="postgres", uri=store.uri, user="u", password=store.password))
    assert isinstance(args["ssl"], ssl.SSLContext) and args["ssl"].check_hostname is True


def test_a_graph_store_uri_of_its_own_takes_the_tls_it_leaves_out(valid_env: dict[str, str], pki: Pki) -> None:
    env = {**valid_env, "GRAPH_STORE": "postgres", "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(pki.ca)}

    def query(uri: str, **over: str) -> dict[str, object]:
        return dict(make_url(load_graph_store_settings({**env, "GRAPH_STORE_URI": uri, **over}).uri).query)

    assert query("postgresql://graph-db:5432/graph") == {"sslmode": "verify-full", "sslrootcert": str(pki.ca)}
    assert query("postgresql://graph-db/graph?sslmode=disable") == {"sslmode": "disable"}
    assert query("postgresql://graph-db/graph?sslmode=verify-ca", PGSSLMODE="", PGSSLROOTCERT="") == {
        "sslmode": "verify-ca",
        "sslrootcert": MOUNTED_CA,
    }
    assert query("postgresql://graph-db/graph", PGSSLMODE="") == {}


def test_the_derived_graph_store_uri_is_quoted(valid_env: dict[str, str]) -> None:
    env = {**valid_env, "GRAPH_STORE": "postgres", "PGSSLMODE": "require"}
    env |= {"DATABASE_HOST": "::1", "DATABASE_NAME": "spec?flow"}
    for name in ("GRAPH_STORE_URI", "GRAPH_STORE_USER", "GRAPH_STORE_PASSWORD"):
        env.pop(name)

    store = load_graph_store_settings(env)
    url, args = connection_url(StoreConfig(backend="postgres", uri=store.uri, user="u", password=store.password))

    assert (url.host, url.database) == ("::1", "spec?flow")
    assert isinstance(args["ssl"], ssl.SSLContext) and args["ssl"].verify_mode is ssl.CERT_NONE


def test_a_graph_store_uri_takes_its_own_ca_bundle(pki: Pki, tmp_path: Path) -> None:
    def args(query: str) -> dict[str, object]:
        return connection_url(
            StoreConfig(backend="postgres", uri=f"postgresql://h/d?{query}", user="u", password=DB_CRED)
        )[1]

    assert isinstance(args(f"sslmode=verify-full&sslrootcert={pki.ca}")["ssl"], ssl.SSLContext)
    pinned = args(f"sslmode=require&sslrootcert={pki.ca}")["ssl"]
    assert isinstance(pinned, ssl.SSLContext) and pinned.verify_mode is ssl.CERT_REQUIRED
    assert args("sslmode=prefer") == {"ssl": "prefer"}
    unusable = (f"sslrootcert={pki.ca}", f"sslmode=prefer&sslrootcert={pki.ca}", "sslmode=verify-full")
    for query in (*unusable, f"sslmode=verify-full&sslrootcert={tmp_path / 'missing.pem'}"):
        with pytest.raises(StoreConfigurationError) as raised:
            args(query)
        assert raised.value.code == "store_uri_invalid"


# Handshakes


async def test_verify_full_connects_with_the_right_ca_and_host_name(pki: Pki) -> None:
    with fake_postgres(pki) as server:
        error = await connect(database(server.port, mode="verify-full", rootcert=str(pki.ca)))

    # The app's context offers the ALPN that direct TLS needs.
    assert refused(error) and server.seen == ["tls"] and server.alpn == ["postgresql"]


async def test_a_blank_or_stray_process_pgsslmode_never_reaches_the_driver(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fake_postgres(pki) as server:
        monkeypatch.setenv("PGSSLMODE", "")
        blank = await connect(database(server.port))
        monkeypatch.setenv("PGSSLMODE", "verify-full")  # the settings came from a mapping without it
        stray = await connect(database(server.port))

    assert refused(blank) and refused(stray) and server.seen == ["tls", "tls"]


async def test_require_checks_the_chain_only_against_a_given_bundle(pki: Pki) -> None:
    # 127.0.0.1 is not in the certificate: require never checks the host name.
    with fake_postgres(pki) as server:
        unchecked = await connect(database(server.port, "127.0.0.1", mode="require"))
        pinned = await connect(database(server.port, "127.0.0.1", mode="require", rootcert=str(pki.ca)))
        wrong_ca = await connect(database(server.port, mode="require", rootcert=str(pki.other_ca)))

    assert refused(unchecked) and refused(pinned) and certificate_refused(wrong_ca)
    assert server.seen.count("tls") == 2


async def test_verify_full_fails_with_another_ca(pki: Pki) -> None:
    with fake_postgres(pki) as server:
        error = await connect(database(server.port, mode="verify-full", rootcert=str(pki.other_ca)))

    assert certificate_refused(error) and "tls" not in server.seen


async def test_verify_full_fails_when_the_host_name_does_not_match(pki: Pki) -> None:
    # The certificate names localhost only; 127.0.0.1 is the same server under a name it does not carry.
    with fake_postgres(pki) as server:
        mismatch = await connect(database(server.port, "127.0.0.1", mode="verify-full", rootcert=str(pki.ca)))
        chain_only = await connect(database(server.port, "127.0.0.1", mode="verify-ca", rootcert=str(pki.ca)))

    assert certificate_refused(mismatch)
    assert refused(chain_only) and server.seen.count("tls") == 1


def test_a_ca_bundle_removed_after_start_fails_the_next_engine(pki: Pki, tmp_path: Path) -> None:
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes(pki.ca.read_bytes())
    settings = database(5432, mode="verify-full", rootcert=str(bundle))
    bundle.unlink()

    with pytest.raises(FileNotFoundError):
        build_engine(settings)


async def test_the_non_tls_modes_keep_working(pki: Pki) -> None:
    with fake_postgres(pki) as server:
        unset = await connect(database(server.port))  # asyncpg's default, prefer: TLS without verification
        disabled = await connect(database(server.port, mode="disable"))

    assert refused(unset) and refused(disabled)
    assert server.seen == ["tls", "plain"]


async def test_the_readiness_probe_and_the_graph_store_use_the_same_tls(pki: Pki) -> None:
    with fake_postgres(pki) as server:
        for rootcert in (pki.ca, pki.other_ca):  # the server refuses every session: the probe is never ok
            engine = build_engine(database(server.port, mode="verify-full", rootcert=str(rootcert)))
            assert not (await health.run_probe("database", health.database_probe(engine))).ok
            await engine.dispose()
        probes = server.seen.count("tls")
        uri = f"postgresql://localhost:{server.port}/specflow?sslmode=verify-full&sslrootcert={pki.ca}"
        graph = build_graph_engine(StoreConfig(backend="postgres", uri=uri, user="u", password=DB_CRED))
        try:
            with pytest.raises(DBAPIError) as raised:
                async with graph.connect():
                    pass
        finally:
            await graph.dispose()

    assert probes == 1  # only the probe with the right CA got past TLS
    assert refused(raised.value) and server.seen.count("tls") == 2


def test_the_migrations_use_the_same_tls(pki: Pki) -> None:
    with fake_postgres(pki) as server:
        settings = RoleSettings(
            role="migrate",
            app=AppSettings(APP_ENV="local"),
            database=database(server.port, mode="verify-full", rootcert=str(pki.ca)),
        )
        config = migrate.build_config(settings)
        assert config.attributes["connect_args"].keys() == {"ssl"}
        with pytest.raises(DBAPIError) as revision:
            asyncio.run(migrate.current_revision(settings.database))
        with pytest.raises(DBAPIError) as upgrade:
            command.upgrade(config, "head")

    assert refused(revision.value) and refused(upgrade.value)
    assert server.seen == ["tls", "tls"]


def test_offline_migrations_read_no_tls_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (("DATABASE_HOST", "offline.invalid"), ("DATABASE_NAME", "d"), ("DATABASE_USER", "u")):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_PASSWORD", DB_CRED)
    monkeypatch.setenv("PGSSLMODE", "verify-full")  # no bundle at the mount path: an online run would refuse
    buffer = StringIO()

    command.upgrade(Config(str(migrate.find_alembic_ini()), output_buffer=buffer), "head", sql=True)

    assert "INSERT INTO alembic_version (version_num) VALUES ('0001_initial')" in buffer.getvalue()


@pytest.fixture
def local_stack_script(tmp_path: Path) -> tuple[Path, list[str]]:
    """The folder that scripts/postgres-tls.sh (``make postgres-tls-certs``) wrote, and its command. Needs
    ``openssl``, as ``make env-local`` does."""

    folder = tmp_path / "postgres-tls"
    script = [shutil.which("sh") or "sh", str(ROOT / "scripts" / "postgres-tls.sh"), str(folder)]
    subprocess.run(script, check=True, capture_output=True, timeout=60)
    return folder, script


async def test_the_local_stack_certificates_pass_verify_full(
    pki: Pki, local_stack_script: tuple[Path, list[str]]
) -> None:
    folder, script = local_stack_script
    names = (
        x509.load_pem_x509_certificate((folder / "server.pem").read_bytes())
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )
    local = Pki(folder / "ca.pem", pki.other_ca, folder / "server.pem", folder / "server.key")

    with fake_postgres(local) as server:
        for host in ("localhost", "127.0.0.1"):
            assert refused(await connect(database(server.port, host, mode="verify-full", rootcert=str(local.ca))))
    again = await asyncio.to_thread(subprocess.run, script, check=True, capture_output=True, text=True, timeout=60)

    assert names.get_values_for_type(x509.DNSName) == ["specflow-postgres", "localhost"]
    assert [str(address) for address in names.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]
    assert server.seen == ["tls", "tls"]
    assert not (folder / "ca.key").exists()  # deleted once the server certificate is signed
    assert "exist" in again.stdout  # a second run keeps the set
