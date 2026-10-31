"""Compose and image checks (AC-13.x static, AC-14.x, AC-16.5).

The local stack (``docker-compose.yml``) mirrors the deployment: the same services (``mcp`` runs
from the api image), images and variables.
Static checks read the repo files and need no docker. The image tests are gated by
``SPECFLOW_TEST_IMAGES=1`` (``make images`` builds the images and runs them): they start
the built images with synthetic, otherwise valid NON-local settings and ``AUTH_MODE=dev``
and expect a refusal that names ``AUTH_MODE``, then a control run that passes. All
values are synthetic; no secret is read, printed or sent anywhere.

The AC-4.2 RSS test (``SPECFLOW_IMAGE_RSS=1``) needs no docker itself: ``make rss-test``
runs it in a throwaway Linux Python container, against a local fake of the GCS
resumable-upload endpoint, so no real object is written.
"""

from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import os
import re
import resource
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

import google_crc32c
import pytest
import yaml
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from runtime_defaults_support import RUNTIME_DEFAULTS
from settings import StorageSettings
from storage.base import CHUNK_SIZE
from storage.gcs import GcsObjectStore

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
OWNED = [
    COMPOSE,
    ROOT / "Makefile",
    ROOT / "backend" / "Makefile",
    ROOT / "backend" / "api.Dockerfile",
    ROOT / "backend" / "worker.Dockerfile",
    ROOT / "backend" / ".dockerignore",
]
API_IMAGE = os.environ.get("SPECFLOW_API_IMAGE", "specflow-api:local")
WORKER_IMAGE = os.environ.get("SPECFLOW_WORKER_IMAGE", "specflow-worker:local")
FRONTEND_IMAGE = os.environ.get("SPECFLOW_FRONTEND_IMAGE", "specflow-frontend:local")

# App service -> (build context, Dockerfile), as in the deployment.
BUILT = {
    "api": ("./backend", "api.Dockerfile"),
    "worker-parse": ("./backend", "worker.Dockerfile"),
    "worker-agents": ("./backend", "worker.Dockerfile"),
    "frontend": ("./frontend", "Dockerfile"),
}
# Database service -> public image.
PUBLIC = {"postgres": "postgres:17.11", "neo4j": "neo4j:5.26.30-community"}
# Local-only platform services this file runs itself (a deployment provides them in its own cluster).
LOCAL_PLATFORM = {
    "temporal",
    "keycloak",
    "rustfs",
    "rustfs-init",
    "langfuse-postgres",
    "langfuse-clickhouse",
    "langfuse-redis",
    "langfuse-minio",
    "langfuse-worker",
    "langfuse-web",
}
BACKEND_SERVICES = ("api", "worker-parse", "worker-agents")
MCP_SERVICE = "mcp"  # the api image with `specflow mcp`; no Temporal, storage or LLM variables

images_enabled = os.environ.get("SPECFLOW_TEST_IMAGES") == "1"
needs_docker = pytest.mark.skipif(
    not (images_enabled and shutil.which("docker")),
    reason="SPECFLOW_TEST_IMAGES=1 and the docker CLI are needed",
)


needs_rss = pytest.mark.skipif(
    os.environ.get("SPECFLOW_IMAGE_RSS") != "1",
    reason="SPECFLOW_IMAGE_RSS=1 (run by `make rss-test` in a Linux container)",
)


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def services() -> dict:
    return compose()["services"]


def env_of(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    assert isinstance(env, dict), "environment must be a mapping"
    return env


# --- static: compose (no docker) -----------------------------------------------------


def test_the_stack_is_the_built_services_the_databases_mcp_and_the_local_platform_services():
    svcs = services()
    assert set(svcs) == set(BUILT) | set(PUBLIC) | LOCAL_PLATFORM | {MCP_SERVICE}
    for name, svc in svcs.items():
        assert svc["container_name"] == f"specflow-{name}", name
    assert compose()["name"] == "specflow"


def test_each_app_service_builds_its_dockerfile_and_databases_use_public_images():
    for name, (context, dockerfile) in BUILT.items():
        build = services()[name]["build"]
        if isinstance(build, str):
            assert (build, "Dockerfile") == (context, dockerfile), name
        else:
            assert build["context"] == context, name
            assert build.get("dockerfile", "Dockerfile") == dockerfile, name
            assert "target" not in build, name  # Infra builds the last stage
    for name, image in PUBLIC.items():
        assert services()[name]["image"] == image and "build" not in services()[name], name


def test_postgres_runs_the_role_script_on_its_first_start():
    mounts = services()["postgres"]["volumes"]
    assert "./backend/postgres-init:/docker-entrypoint-initdb.d:ro" in mounts
    assert (ROOT / "backend" / "postgres-init" / "10-specflow-roles.sh").is_file()


def test_the_file_uses_no_anchors_merge_keys_or_extension_fields():
    text = COMPOSE.read_text()
    assert not re.search(r"^x-", text, re.MULTILINE)
    # Only the local-only Langfuse services share one environment block through an anchor.
    current = ""
    for line in text.splitlines():
        key = re.match(r"^  ([\w-]+):\s*$", line)
        if key:
            current = key.group(1)
        code = line.split("#", 1)[0]
        if re.search(r"<<:|(^|\s)[&*][A-Za-z][\w-]*", code):
            assert current.startswith("langfuse-"), (current, line)


DEFAULT_SERVICES = {
    "postgres",
    "neo4j",
    "rustfs",
    "rustfs-init",
    "api",
    "mcp",
    "worker-parse",
    "worker-agents",
    "frontend",
}
LANGFUSE_SERVICES = {f"langfuse-{n}" for n in ("postgres", "clickhouse", "redis", "minio", "worker", "web")}


def test_temporal_langfuse_and_keycloak_are_optional_profiles():
    """D32: the default stack starts no Temporal, Langfuse or Keycloak; WITH=... turns them on."""

    svcs = services()
    by_profile = {}
    for name, svc in svcs.items():
        for profile in svc.get("profiles", []):
            by_profile.setdefault(profile, set()).add(name)
    assert by_profile == {"temporal": {"temporal"}, "langfuse": LANGFUSE_SERVICES, "keycloak": {"keycloak"}}
    assert {n for n, svc in svcs.items() if "profiles" not in svc} == DEFAULT_SERVICES


def test_the_app_reaches_an_external_temporal_and_langfuse_by_default():
    for name in BACKEND_SERVICES:
        svc = services()[name]
        env = env_of(svc)
        assert env["TEMPORAL_ADDRESS"] == "${TEMPORAL_ADDRESS:-host.docker.internal:7233}", name
        assert "host.docker.internal:host-gateway" in svc["extra_hosts"], name
        assert env["TEMPORAL_NAMESPACE"] == "${TEMPORAL_NAMESPACE:-specflow-local}", name
    for name in ("api", "worker-agents"):  # tracing off while both keys are empty
        env = env_of(services()[name])
        for key in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            assert env[key] == f"${{{key}:-}}", (name, key)


def test_make_up_with_selects_the_profiles_and_points_the_app_at_them():
    makefile = (ROOT / "Makefile").read_text()
    assert "COMPOSE_PROFILES=temporal,langfuse,keycloak" in makefile  # down, ps, logs cover every profile
    for needle in (
        "MCP_AUTH_MODE=oidc",
        "MCP_OIDC_ISSUER=$(KEYCLOAK_ISSUER)",
        "KEYCLOAK_ISSUER := http://localhost:8180/realms/specflow",
        "specflow-keycloak:8080/realms/specflow/protocol/openid-connect/certs",
        "-u MCP_AUTH_MODE",
        "-u MCP_OIDC_ISSUER",
        "-u MCP_AUTH_JWKS_URL",
        "-u MCP_AUTH_DEV_SIGNING_KEY",
    ):
        assert needle in makefile, needle
    assert re.search(r"MCP_AUTH_DEV_SIGNING_KEY=(\s|$)", makefile, re.MULTILINE)  # an empty dev key under oidc
    # The shared names are never assigned: api and frontend read them too and would leave dev login.
    assert not re.search(r"(?<![A-Z_])(AUTH_MODE|OIDC_ISSUER|AUTH_JWKS_URL|AUTH_DEV_SIGNING_KEY)=", makefile)
    assert "specflow-temporal:7233" in makefile
    assert "LANGFUSE_BASE_URL=http://specflow-langfuse-web:3000" in makefile
    recipe_uses_all_profiles = r"^{}:.*\n(?:\t@?if[^\n]*\n(?:\t[^\n]*\n)*?)?\t[^\n]*\$\(COMPOSE_ALL\)"
    for target in ("down", "down-volumes", "logs", "ps"):
        assert re.search(recipe_uses_all_profiles.format(re.escape(target)), makefile, re.MULTILINE), target


def test_every_service_has_a_memory_limit():
    missing = [name for name, svc in services().items() if "mem_limit" not in svc]
    assert missing == []


def test_every_published_port_is_loopback_and_never_3000_8080_or_5432():
    """AC-13.2. The only 3000 is the local-only Langfuse UI; the frontend stays on 3001."""

    seen = []
    for name, svc in services().items():
        for port in svc.get("ports", []):
            text = str(port)
            seen.append((name, text))
            assert text.startswith("127.0.0.1:"), (name, text)
            host_port = text.split(":")[1]
            if (name, text) != ("langfuse-web", "127.0.0.1:3000:3000"):
                assert not re.fullmatch(r"(3000|8080|5432)", host_port), (name, text)
    expected = {"postgres", "neo4j", "api", "frontend", "temporal", "langfuse-web", "rustfs", "keycloak", "mcp"}
    assert {name for name, _ in seen} == expected
    assert services()["frontend"]["ports"] == ["127.0.0.1:${FRONTEND_PORT:-3001}:3000"]
    assert services()["rustfs"]["ports"] == ["127.0.0.1:${RUSTFS_HOST_PORT:-19000}:9000"]
    assert services()["keycloak"]["ports"] == ["127.0.0.1:8180:8080"]
    assert services()["mcp"]["ports"] == ["127.0.0.1:8002:8002"]


def test_services_list_their_variables_explicitly():
    for name, svc in services().items():
        assert "env_file" not in svc, name
        if name != "langfuse-redis":  # configured through its command line only
            assert env_of(svc), name


def test_in_network_values_use_the_deployment_host_names():
    for name in BACKEND_SERVICES:
        env = env_of(services()[name])
        assert env["DATABASE_HOST"] == "specflow-postgres", name
        assert env["DATABASE_NAME"] == env["DATABASE_USER"] == "specflow", name
    for name in ("api", "worker-agents"):
        assert env_of(services()[name])["NEO4J_URI"] == "bolt://specflow-neo4j:7687", name
    assert env_of(services()["frontend"])["API_INTERNAL_URL"] == "http://specflow-api:8000"


def test_secret_variables_reach_only_their_roles():
    """AC-13.6, from the compose file (names only)."""

    names = {name: set(env_of(svc)) for name, svc in services().items()}

    def holders(var):
        return {s for s, n in names.items() if var in n}

    assert holders("DATABASE_AGENT_PASSWORD") == {"postgres", "worker-agents"}
    # The api runs the graph build in-process (POST /projects/{pid}/graph_build).
    assert holders("LLM_GATEWAY_API_KEY") == {"api", "worker-agents"}
    assert holders("NEO4J_PASSWORD") == {"api", "worker-agents", "mcp"}
    assert holders("DATABASE_PASSWORD") == {"postgres", "api", "worker-parse", "worker-agents", "mcp"}
    assert holders("POSTGRES_PASSWORD") == {"postgres", "langfuse-postgres"}  # the latter is its own store
    assert holders("AUTH_DEV_SIGNING_KEY") == {"api", "frontend", "mcp"}
    assert holders("KC_BOOTSTRAP_ADMIN_PASSWORD") == {"keycloak"}
    allowed = {"SESSION_SECRET", "OIDC_CLIENT_SECRET", "AUTH_DEV_SIGNING_KEY"}
    secrets = {
        var for var in names["frontend"] if re.search(r"(PASSWORD|SECRET|API_KEY|SIGNING_KEY|PUBLIC_KEY|TLS_KEY)$", var)
    }
    assert secrets <= allowed, secrets - allowed


def test_base_images_are_pinned_to_exact_tags():
    """AC-14.4 (no latest, no floating major) for the database images and every Dockerfile."""

    pin = re.compile(r"^[\w./-]+:\d+\.\d+(\.\d+)?[\w.-]*$")
    for name in PUBLIC:
        assert pin.match(services()[name]["image"]), name
    for context, dockerfile in set(BUILT.values()):
        path = ROOT / context / dockerfile
        bases = re.findall(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", path.read_text(), re.MULTILINE)
        stages = {stage for _, stage in bases if stage}
        for image, _ in bases:
            assert image in stages or pin.match(image), (path.name, image)
    for name in ("api.Dockerfile", "worker.Dockerfile"):
        assert re.search(r"pip install uv==\d+\.\d+\.\d+\b", (ROOT / "backend" / name).read_text()), name


def test_the_cloud_profile_mount_no_adc_and_no_log_files_for_the_backend_services():
    """The cloud profile is read-only; no ADC file, no log volume, no file-log env."""

    text = COMPOSE.read_text()
    for forbidden in (
        "application_default_credentials",
        "GCLOUD_ADC_FILE",
        "/var/run/gcp",
        "logs/compose",
        "/app/logs",
        "LOGS_RETENTION_DAYS",
        "LOGS_STDOUT_FORMAT",
        "STORAGE_BUCKET",
    ):
        assert forbidden not in text, forbidden
    for name in BACKEND_SERVICES:
        svc = services()[name]
        profile = [m for m in svc["volumes"] if m["target"] == "/etc/specflow/cloud.yaml"]
        assert len(profile) == 1 and profile[0]["read_only"] is True, name
        assert profile[0]["source"].endswith("config/cloud.local.yaml"), name
        # The api also keeps the graph build state on a named volume.
        expected = 2 if name == "api" else 1
        assert len(svc["volumes"]) == expected, name
        env = env_of(svc)
        assert env["SPECFLOW_CONFIG_FILE"] == "/etc/specflow/cloud.yaml", name
        assert env["AWS_ACCESS_KEY_ID"].startswith("${RUSTFS_ACCESS_KEY:?"), name
        assert env["AWS_SECRET_ACCESS_KEY"].startswith("${RUSTFS_SECRET_KEY:?"), name
        assert env["LOGS_STDOUT_LEVEL"] == "INFO", name


def test_rustfs_is_pinned_needs_both_keys_and_has_a_one_shot_bucket_init():
    svcs = services()
    rustfs = svcs["rustfs"]
    assert re.fullmatch(r"rustfs/rustfs:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", rustfs["image"])
    assert env_of(rustfs)["RUSTFS_ACCESS_KEY"].startswith("${RUSTFS_ACCESS_KEY:?")
    assert env_of(rustfs)["RUSTFS_SECRET_KEY"].startswith("${RUSTFS_SECRET_KEY:?")
    assert "healthcheck" in rustfs and rustfs["volumes"] == ["specflow-rustfs-data:/data"]
    init = svcs["rustfs-init"]
    assert init["restart"] == "no"
    assert init["depends_on"] == {"rustfs": {"condition": "service_healthy"}}
    assert env_of(init)["INIT_BUCKET"] == "specflow-local"


def test_the_mcp_role_runs_the_api_image_in_dev_mode_unless_make_switches_it():
    """FR-MCP-OPS-08: dev mode by default; WITH=keycloak sets the MCP_* names, only this service changes."""

    svcs = services()
    svc = svcs[MCP_SERVICE]
    env = env_of(svc)
    port = RUNTIME_DEFAULTS["MCP_PORT"]
    assert svc["build"] == svcs["api"]["build"]
    assert svc["image"] == svcs["api"]["image"]
    assert svc["command"] == ["specflow", "mcp"]
    assert "volumes" not in svc and "extra_hosts" not in svc
    assert f"http://127.0.0.1:{port}/readyz" in " ".join(svc["healthcheck"]["test"])
    assert env["APP_ENV"] == "${APP_ENV:-local}"
    assert env["MCP_PORT"] == str(port)
    assert env["MCP_RESOURCE_URL"] == f"http://localhost:{port}/mcp"
    assert env["DATABASE_HOST"] == "specflow-postgres"
    assert env["NEO4J_URI"] == "bolt://specflow-neo4j:7687"
    assert env["AUTH_MODE"] == "${MCP_AUTH_MODE:-dev}"
    # Set-but-empty wins. The repo has no compose-interpolation renderer for unit tests (and `make check` takes no
    # docker dependency), so unset/empty/set rendering is proven by the recorded stack evidence (06-handoff.md).
    assert env["AUTH_DEV_SIGNING_KEY"] == "${MCP_AUTH_DEV_SIGNING_KEY-${AUTH_DEV_SIGNING_KEY:-}}"
    assert env["OIDC_ISSUER"] == "${MCP_OIDC_ISSUER:-}"
    assert env["AUTH_JWKS_URL"] == "${MCP_AUTH_JWKS_URL:-}"
    prefixes = ("TEMPORAL_", "AWS_", "LLM_", "LANGFUSE_", "GRAPH_", "AGENT_", "DATABASE_AGENT_")
    assert [key for key in env if key.startswith(prefixes) or key == "SPECFLOW_CONFIG_FILE"] == []
    claims = {"AUTH_SUBJECT_CLAIM", "AUTH_CLIENT_ID_CLAIM", "AUTH_EMAIL_CLAIM", "AUTH_EMAIL_VERIFIED_CLAIM"}
    claims |= {"AUTH_TOKEN_TYPE", "AUTH_IGNORED_AUDIENCES"}  # an empty fallback would switch a check off
    assert [name for name, other in svcs.items() if claims & set(env_of(other))] == []
    assert sorted(key for key, value in env.items() if ":?" in str(value)) == ["DATABASE_PASSWORD", "NEO4J_PASSWORD"]
    for name in ("api", "frontend"):
        assert env_of(svcs[name])["AUTH_MODE"] == "${AUTH_MODE:-dev}", name  # the web app and the API keep dev login


def test_keycloak_is_pinned_optional_and_imports_the_realm_read_only():
    """Local OIDC provider of the MCP QA tiers: digest-pinned, realm file mounted read-only, no secret in compose."""

    svc = services()["keycloak"]
    assert re.fullmatch(r"quay\.io/keycloak/keycloak:26\.4\.\d+@sha256:[0-9a-f]{64}", svc["image"])
    assert svc["command"] == ["start-dev", "--import-realm"]
    assert svc["volumes"] == ["./config/keycloak:/opt/keycloak/data/import:ro"]
    assert "healthcheck" in svc
    env = env_of(svc)
    admin = [value for key, value in env.items() if key.endswith("_ADMIN_PASSWORD")]
    assert admin == ["${KEYCLOAK_ADMIN_PASSWORD:-}"]  # an empty fallback, never a literal
    assert env["KC_HOSTNAME"] == "http://localhost:8180"
    assert env["KC_HEALTH_ENABLED"] == "true"
    assert not any(":?" in str(value) for value in env.values())  # a required variable breaks older .env.local files
    assert "read_only" not in svc  # start-dev writes its H2 data under /opt/keycloak/data
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "KEYCLOAK_QA_PASSWORD" not in compose
    assert "KEYCLOAK_WEB_CLIENT_SECRET" not in compose
    assert (ROOT / "config/keycloak/specflow-realm.json").is_file()


def test_the_backend_services_run_on_a_read_only_root_filesystem():
    """AC-15: read_only on the three backend services and mcp, as on Kubernetes; tmpfs /tmp on the two workers only."""

    svcs = services()
    readonly = {name for name, svc in svcs.items() if svc.get("read_only") is True}
    assert readonly == set(BACKEND_SERVICES) | {MCP_SERVICE}
    assert svcs["worker-parse"]["tmpfs"] == [f"{PurePosixPath('/', 'tmp')}:size=1g"]
    assert svcs["worker-agents"]["tmpfs"] == [f"{PurePosixPath('/', 'tmp')}:size=256m"]
    assert "tmpfs" not in svcs["api"]
    assert "tmpfs" not in svcs[MCP_SERVICE]
    assert not (ROOT / "docker-compose.readonly.yml").exists()
    makefile = (ROOT / "Makefile").read_text()
    assert "logs/compose" not in makefile
    assert "up-readonly" not in makefile


def test_the_start_order_follows_the_blueprint():
    svcs = services()
    assert svcs["api"]["depends_on"] == {
        "postgres": {"condition": "service_healthy"},
        "neo4j": {"condition": "service_healthy"},
        # Present only when the temporal / langfuse profile is on.
        "temporal": {"condition": "service_healthy", "required": False},
        "langfuse-web": {"condition": "service_healthy", "required": False},
        "rustfs-init": {"condition": "service_completed_successfully"},
    }
    for name in ("worker-parse", "worker-agents"):
        assert svcs[name]["depends_on"] == {
            "api": {"condition": "service_healthy"},
            "rustfs-init": {"condition": "service_completed_successfully"},
        }, name
    assert svcs["frontend"]["depends_on"] == {"api": {"condition": "service_healthy"}}
    healthy = {"condition": "service_healthy"}  # api has migrated; no keycloak (JWKS on first request)
    assert svcs[MCP_SERVICE]["depends_on"] == {"postgres": healthy, "neo4j": healthy, "api": healthy}


def test_each_worker_polls_one_queue_and_drains_on_stop():
    svcs = services()
    assert env_of(svcs["worker-parse"])["WORKER_QUEUES"] == "parse"
    assert env_of(svcs["worker-agents"])["WORKER_QUEUES"] == "agents"
    for name in ("worker-parse", "worker-agents"):
        assert svcs[name]["stop_grace_period"] == "150s", name
    for name in BACKEND_SERVICES:
        env = env_of(svcs[name])
        assert env["TEMPORAL_QUEUE_PARSE"] == "specflow-parse", name
        assert env["TEMPORAL_QUEUE_AGENTS"] == "specflow-agents", name


def test_no_host_absolute_path_in_the_files_this_step_owns():
    pattern = re.compile(r"/[U]sers/|/private/|/home/(?!specflow)|[A-Z]:\\\\")
    for path in [*OWNED, Path(__file__)]:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if path == Path(__file__) and "pattern = re.compile" in line:
                continue
            assert not pattern.search(line), f"{path.name}:{number}"


def test_the_backend_dockerignore_keeps_secrets_tests_and_caches_out():
    lines = {
        line.strip()
        for line in (ROOT / "backend" / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {".venv", ".env*", "tests", "logs", "__pycache__"} <= lines


def test_the_backend_dockerfiles_have_the_runtime_contract():
    """AC-14.1 (static part) for the api and worker images."""

    for name in ("api.Dockerfile", "worker.Dockerfile"):
        text = (ROOT / "backend" / name).read_text()
        runtime = text[text.rindex("\nFROM ") :]
        assert "uv sync --frozen --no-dev" in text and "--extra gcp --extra aws" in text, name
        assert "USER 10001:10001" in runtime, name
        assert "LOGS_" not in text and "/app/logs" not in text, name
        assert "COPY tests" not in text and ".env" not in runtime, name
    api = (ROOT / "backend" / "api.Dockerfile").read_text()
    assert 'CMD ["sh", "-c", "specflow migrate && exec specflow api"]' in api
    assert "COPY alembic.ini" in api and "COPY migrations" in api
    worker = (ROOT / "backend" / "worker.Dockerfile").read_text()
    assert 'CMD ["specflow", "worker"]' in worker and "EXPOSE 8001" in worker


# --- images (docker, SPECFLOW_TEST_IMAGES=1) -------------------------------------------

DEPLOYED_BACKEND_ENV = {
    "APP_ENV": "dev",
    "API_PORT": "8000",
    "DATABASE_HOST": "db.invalid",
    "DATABASE_PORT": "5432",
    "DATABASE_NAME": "app",
    "DATABASE_USER": "app_user",
    "DATABASE_PASSWORD": "synthetic-db-password-1",
    "DATABASE_AGENT_USER": "agent_user",
    "DATABASE_AGENT_PASSWORD": "synthetic-agent-password-1",
    "OIDC_ISSUER": "https://sso.invalid/realms/app",
    "AUTH_JWKS_URL": "https://sso.invalid/realms/app/certs",
    "AUTH_API_AUDIENCE": "specflow-api",
    "AUTH_API_CLIENT_IDS": "specflow-web",
    "AUTH_ALLOWED_EMAIL_DOMAINS": "example.com",
    "STORAGE_BACKEND": "gcs",
    "STORAGE_BUCKET": "synthetic-bucket",
    "STORAGE_PREFIX": "dev/",
    "GOOGLE_CLOUD_PROJECT": "synthetic-project",
    "TEMPORAL_ADDRESS": "temporal.invalid:7233",
    "TEMPORAL_NAMESPACE": "synthetic",
    "NEO4J_URI": "bolt://neo4j.invalid:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "synthetic-neo4j-password-1",
    "AGENT_MODEL": "synthetic/model",
    "LLM_GATEWAY_URL": "https://gateway.invalid/openai",
    "LLM_GATEWAY_API_KEY": "synthetic-gateway-key-1",
}
DEPLOYED_FRONTEND_ENV = {
    "APP_ENV": "dev",
    "PUBLIC_URL": "https://app.invalid",
    "API_INTERNAL_URL": "http://api.invalid:8000",
    "SESSION_SECRET": "s" * 40,
    "OIDC_ISSUER": "https://sso.invalid/realms/app",
    "OIDC_CLIENT_ID": "specflow-web",
    "OIDC_CLIENT_SECRET": "synthetic-oidc-client-secret-1",
}
DEV_KEY = {"AUTH_DEV_SIGNING_KEY": "k" * 40}


def docker_run(
    image: str,
    env: dict[str, str],
    *args: str,
    entrypoint: str | None = None,
    read_only: bool = False,
):
    command = ["docker", "run", "--rm", "--network", "none"]
    if read_only:
        command.append("--read-only")
    if entrypoint:
        command += ["--entrypoint", entrypoint]
    for name, value in env.items():
        command += ["-e", f"{name}={value}"]
    command += [image, *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)


def shown(result) -> str:
    return result.stdout + result.stderr


@needs_docker
def test_the_api_image_refuses_dev_auth_outside_local_and_names_auth_mode():
    """AC-14.3: complete valid non-local env + AUTH_MODE=dev -> non-zero, AUTH_MODE named."""

    env = {**DEPLOYED_BACKEND_ENV, "AUTH_MODE": "dev", **DEV_KEY}
    result = docker_run(API_IMAGE, env, "specflow", "api")
    assert result.returncode != 0
    assert "AUTH_MODE" in shown(result)
    assert "synthetic-db-password-1" not in shown(result)


@needs_docker
def test_the_backend_control_runs_pass_check_config():
    env = {**DEPLOYED_BACKEND_ENV, "AUTH_MODE": "oidc"}
    result = docker_run(API_IMAGE, env, "specflow", "api", "--check-config")
    assert result.returncode == 0, shown(result)[-400:]
    for queues in ("parse", "agents"):
        worker = docker_run(
            WORKER_IMAGE,
            {**env, "WORKER_QUEUES": queues},
            "specflow",
            "worker",
            "--check-config",
        )
        assert worker.returncode == 0, (queues, shown(worker)[-400:])


MCP_ONLY_ENV = {
    "APP_ENV": "dev",
    "DATABASE_HOST": "db.invalid",
    "DATABASE_NAME": "app",
    "DATABASE_USER": "app_user",
    "DATABASE_PASSWORD": "synthetic-db-password-1",
    "NEO4J_URI": "bolt://neo4j.invalid:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "synthetic-neo4j-password-1",
    "OIDC_ISSUER": "https://sso.invalid/realms/app",
    "AUTH_JWKS_URL": "https://sso.invalid/realms/app/certs",
    "AUTH_ALLOWED_EMAIL_DOMAINS": "example.com",
    "MCP_RESOURCE_URL": "https://mcp.invalid/mcp",
    "AUTH_MODE": "oidc",
}


@needs_docker
def test_the_mcp_role_control_run_passes_check_config_with_only_its_own_variables():
    """FR-MCP-OPS-08: the api image runs `specflow mcp --check-config` without Temporal, storage or LLM variables."""

    assert not {"TEMPORAL_ADDRESS", "STORAGE_BACKEND", "AGENT_MODEL", "LLM_GATEWAY_URL"} & set(MCP_ONLY_ENV)
    result = docker_run(API_IMAGE, MCP_ONLY_ENV, "specflow", "mcp", "--check-config")
    assert result.returncode == 0, shown(result)[-400:]


@needs_docker
def test_the_frontend_runtime_image_refuses_dev_auth_and_names_auth_mode():
    env = {**DEPLOYED_FRONTEND_ENV, "AUTH_MODE": "dev", **DEV_KEY}
    result = docker_run(FRONTEND_IMAGE, env)
    assert result.returncode != 0
    assert "AUTH_MODE" in shown(result)


@needs_docker
def test_the_frontend_control_run_passes_check_env():
    env = {**DEPLOYED_FRONTEND_ENV, "AUTH_MODE": "oidc"}
    result = docker_run(FRONTEND_IMAGE, env, "scripts/check-env.mjs", entrypoint="node")
    assert result.returncode == 0, shown(result)[-400:]


@needs_docker
@pytest.mark.parametrize("image", [API_IMAGE, WORKER_IMAGE])
def test_the_backend_image_layout_and_user(image):
    """AC-14: non-root 10001, no tests, no env files, no deploy/, no /app/logs, specflow importable."""

    script = (
        'set -e; test "$(id -u)" = 10001; test ! -e /app/tests; test ! -e /app/deploy; '
        "ls -A /app | grep -E '^\\.env' && exit 1; "
        "test ! -e /app/logs; touch /tmp/w \"$HOME/w\"; python -c 'import cli'; echo layout-ok"
    )
    result = docker_run(image, {}, "-c", script, entrypoint="sh")
    assert result.returncode == 0 and "layout-ok" in result.stdout, shown(result)[-400:]


@needs_docker
@pytest.mark.parametrize("image", [API_IMAGE, WORKER_IMAGE])
def test_the_backend_image_logs_json_to_stdout_on_a_read_only_filesystem(image):
    """AC-14: --read-only, no network: bootstrap, log one event, rc 0, JSON line, no logger stderr."""

    code = (
        "from logger.logger import INFO, get_logger, log_event; "
        "log_event(get_logger('image.readonly'), INFO, 'image.readonly.ok')"
    )
    result = docker_run(image, {}, "-c", code, entrypoint="python", read_only=True)
    assert result.returncode == 0, shown(result)[-400:]
    record = json.loads(result.stdout.strip().splitlines()[-1])
    assert "severity" in record
    assert "[central-logger]" not in result.stderr


@needs_docker
def test_the_api_image_carries_the_migrations_and_the_three_roles():
    script = "test -f /app/alembic.ini && test -d /app/migrations/versions && specflow --help"
    result = docker_run(API_IMAGE, {}, "-c", script, entrypoint="sh")
    assert result.returncode == 0, shown(result)[-400:]
    assert all(role in result.stdout for role in ("api", "worker", "migrate"))


# --- AC-4.2 on Linux: 100 MiB through the real GCS writer, fake transport ---------------

MiB = 1024 * 1024


class _FakeGcs(BaseHTTPRequestHandler):
    """Just enough of the resumable-upload protocol: discards the bytes, counts them."""

    received = 0
    crc = None

    def log_message(self, *_args):
        return

    def _reply(self, code, headers=None, body=b""):
        self.send_response(code)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        host = self.headers["Host"]
        self._reply(200, {"Location": f"http://{host}/session/1"})

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        remaining = length
        while remaining:
            piece = self.rfile.read(min(remaining, 256 * 1024))
            remaining -= len(piece)
            _FakeGcs.crc.update(piece)
        _FakeGcs.received += length
        total = self.headers.get("Content-Range", "").rsplit("/", 1)[-1]
        if total.isdigit():
            body = json.dumps(
                {
                    "name": "x",
                    "bucket": "b",
                    "size": total,
                    "generation": "1",
                    "crc32c": base64.b64encode(_FakeGcs.crc.digest()).decode(),
                }
            ).encode()
            self._reply(200, {"Content-Type": "application/json"}, body)
        else:
            self._reply(308, {"Range": f"bytes=0-{_FakeGcs.received - 1}"})


def _serve(port_queue):
    _FakeGcs.crc = google_crc32c.Checksum()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGcs)
    port_queue.put(server.server_address[1])
    server.serve_forever()


def _peak_mib() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / MiB if sys.platform == "darwin" else raw / 1024


@needs_rss
async def test_ac_4_2_rss_for_100_mib_with_four_mib_chunks_against_a_fake_transport(
    record_property,
):
    """Same measurement as the live GCS test (peak RSS growth over the write loop), no real object."""

    assert 4 * MiB == CHUNK_SIZE
    queue = multiprocessing.get_context("spawn" if sys.platform == "darwin" else "fork").Queue()
    server = multiprocessing.get_context("spawn" if sys.platform == "darwin" else "fork").Process(
        target=_serve, args=(queue,), daemon=True
    )
    server.start()
    port = queue.get(timeout=20)
    try:
        client = await asyncio.to_thread(
            storage.Client,
            project="synthetic",
            credentials=AnonymousCredentials(),
            client_options={"api_endpoint": f"http://127.0.0.1:{port}"},
        )
        store = GcsObjectStore(
            StorageSettings(
                STORAGE_BUCKET="b",
                STORAGE_PREFIX="dev/local/pytest/fake/",
                GOOGLE_CLOUD_PROJECT="synthetic",
            ),
            client=client,
        )
        writer = await store.open_writer("big/100mib.bin", content_type="application/octet-stream")
        block = os.urandom(MiB)
        # Warm-up: the first chunk starts the session and loads the transport modules.
        base = _peak_mib()
        for _ in range(100):
            await writer.write(block)
        stored = await writer.close()
        growth = _peak_mib() - base
    finally:
        server.terminate()
    record_property("rss_growth_mib", round(growth, 1))
    sys.stderr.write(f"\nAC-4.2 RSS growth for 100 MiB (fake transport, {sys.platform}): {growth:.1f} MiB\n")
    assert stored.size == 100 * MiB
    assert growth < 64, f"RSS grew {growth:.1f} MiB for 100 MiB (limit 64)"


# --- SEC-M8-2: `make up WITH=keycloak` refuses an env file without the admin password ---------


def _keycloak_check(tmp_path: Path, env_text: str, *, with_: str = "keycloak") -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "scratch.env"
    env_file.write_text(env_text)  # synthetic values only
    return subprocess.run(
        [
            shutil.which("make") or "make",
            "--no-print-directory",
            "check-keycloak-env",
            f"WITH={with_}",
            f"ENV_FILE={env_file}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "env_text", ["OTHER=1\n", "OTHER=1\nKEYCLOAK_ADMIN_PASSWORD=\n", 'KEYCLOAK_ADMIN_PASSWORD=""\n']
)
def test_up_with_keycloak_fails_early_without_the_admin_password(tmp_path, env_text):
    result = _keycloak_check(tmp_path, env_text)
    assert result.returncode != 0
    assert "KEYCLOAK_ADMIN_PASSWORD" in result.stderr and "make env-local" in result.stderr


def test_up_with_keycloak_passes_the_check_with_the_admin_password_and_never_prints_it(tmp_path):
    result = _keycloak_check(tmp_path, "KEYCLOAK_ADMIN_PASSWORD=synthetic-not-a-secret\n")
    assert result.returncode == 0
    assert "synthetic-not-a-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize("with_", ["", "temporal,langfuse"])
def test_other_profiles_do_not_need_the_keycloak_admin_password(tmp_path, with_):
    assert _keycloak_check(tmp_path, "OTHER=1\n", with_=with_).returncode == 0


def test_up_depends_on_the_keycloak_check_and_compose_keeps_the_empty_default():
    assert re.search(r"^up: .*check-keycloak-env", (ROOT / "Makefile").read_text(), re.MULTILINE)
    assert "${KEYCLOAK_ADMIN_PASSWORD:-}" in COMPOSE.read_text()
