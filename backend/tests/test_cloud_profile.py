"""The cloud profile file: schema, precedence (SPL-CFG-02) and the error table (SPL-CFG-03)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cloud_profile import load_profile
from settings import ConfigurationError, StorageSettings, load_settings

ROOT = Path(__file__).resolve().parents[2]
AWS_FILE = """\
cloud: aws
services:
  object_storage:
    bucket: file-bucket
    prefix: file/
    s3: {region: eu-west-1}
"""


@pytest.fixture
def storage_env(valid_env: dict[str, str]) -> dict[str, str]:
    """A valid environment with no storage variable, so a test sets what it checks."""

    for name in ("STORAGE_BACKEND", "STORAGE_BUCKET", "STORAGE_PREFIX", "GOOGLE_CLOUD_PROJECT"):
        valid_env.pop(name)
    return valid_env


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "cloud.yaml"
    path.write_text(text)
    return str(path)


def storage(env: dict[str, str]) -> StorageSettings:
    found = load_settings("api", env).storage
    assert found is not None
    return found


def refused(env: dict[str, str]) -> tuple[str, ...]:
    with pytest.raises(ConfigurationError) as caught:
        load_settings("api", env)
    return caught.value.variables


# --- precedence (SPL-CFG-02) ---------------------------------------------------------


def test_case_a_no_file_env_only_gives_gcs(storage_env):
    storage_env.update(STORAGE_BUCKET="env-bucket", STORAGE_PREFIX="dev/", GOOGLE_CLOUD_PROJECT="proj")
    found = storage(storage_env)
    assert (found.backend, found.bucket, found.cloud) == ("gcs", "env-bucket", None)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_config_file_variable_means_no_file(storage_env, blank):
    storage_env.update(SPECFLOW_CONFIG_FILE=blank, STORAGE_BUCKET="b", STORAGE_PREFIX="dev/")
    assert storage(storage_env).backend == "gcs"


def test_case_b_file_cloud_aws_without_env_gives_s3(storage_env, tmp_path):
    storage_env["SPECFLOW_CONFIG_FILE"] = write(tmp_path, AWS_FILE)
    found = storage(storage_env)
    assert (found.backend, found.bucket, found.prefix) == ("s3", "file-bucket", "file/")
    assert (found.cloud, found.s3_region, found.s3_endpoint_url, found.s3_addressing) == (
        "aws",
        "eu-west-1",
        None,
        "auto",
    )


def test_case_c_env_provider_that_the_cloud_refuses_is_named(storage_env, tmp_path):
    storage_env.update(SPECFLOW_CONFIG_FILE=write(tmp_path, AWS_FILE), STORAGE_BACKEND="gcs")
    assert refused(storage_env) == ("STORAGE_BACKEND",)


def test_case_d_env_bucket_beats_file_bucket(storage_env, tmp_path):
    storage_env.update(SPECFLOW_CONFIG_FILE=write(tmp_path, AWS_FILE), STORAGE_BUCKET="env-bucket")
    found = storage(storage_env)
    assert (found.bucket, found.prefix) == ("env-bucket", "file/")


def test_case_e_init_kwarg_beats_env():
    found = StorageSettings(STORAGE_BUCKET="kwarg", STORAGE_PREFIX="p/")
    assert found.bucket == "kwarg"  # the init source is the only source of a binding
    assert StorageSettings.variables().count("cloud") == 0


def test_env_s3_values_beat_file_values_and_blank_env_keeps_the_file(storage_env, tmp_path):
    storage_env.update(
        SPECFLOW_CONFIG_FILE=write(tmp_path, AWS_FILE),
        AWS_REGION="us-east-2",
        AWS_ENDPOINT_URL_S3="http://localhost:9000",
        STORAGE_S3_ADDRESSING="path",
        STORAGE_BACKEND="  ",
    )
    found = storage(storage_env)
    assert (found.s3_region, found.s3_endpoint_url, found.s3_addressing, found.backend) == (
        "us-east-2",
        "http://localhost:9000",
        "path",
        "s3",
    )


def test_the_file_sets_the_gcs_project(storage_env, tmp_path):
    text = "cloud: gcp\nservices: {object_storage: {bucket: b, prefix: p/, gcs: {project: proj-1}}}\n"
    storage_env["SPECFLOW_CONFIG_FILE"] = write(tmp_path, text)
    found = storage(storage_env)
    assert (found.backend, found.google_cloud_project) == ("gcs", "proj-1")


# --- error table (SPL-CFG-03) --------------------------------------------------------


def test_a_missing_file_names_the_file_variable(storage_env, tmp_path):
    storage_env["SPECFLOW_CONFIG_FILE"] = str(tmp_path / "absent.yaml")
    assert "SPECFLOW_CONFIG_FILE" in refused(storage_env)


@pytest.mark.parametrize("text", ["cloud: [unclosed", "- a\n- b\n", "just text\n"])
def test_invalid_yaml_or_not_a_mapping_names_the_file_variable(tmp_path, text):
    assert load_profile(write(tmp_path, text)) == (None, ["SPECFLOW_CONFIG_FILE"])


@pytest.mark.parametrize("text", ["services: {}\n", "cloud: azure\n"])
def test_a_missing_or_bad_cloud_names_cloud(tmp_path, text):
    assert load_profile(write(tmp_path, text)) == (None, ["cloud"])


def test_an_unknown_provider_names_the_backend_variable(tmp_path):
    text = "cloud: aws\nservices: {object_storage: {provider: azure}}\n"
    assert load_profile(write(tmp_path, text)) == (None, ["STORAGE_BACKEND"])


def test_an_unknown_key_is_named_by_its_dotted_path(tmp_path):
    text = "cloud: aws\nservices: {object_storage: {bukcet: x, s3: {access_key: y}}}\nqueues: {}\n"
    _, names = load_profile(write(tmp_path, text))
    assert sorted(names) == sorted(
        ["services.object_storage.bukcet", "services.object_storage.s3.access_key", "queues"]
    )


def test_bucket_and_prefix_are_required_after_the_merge(storage_env, tmp_path):
    storage_env["SPECFLOW_CONFIG_FILE"] = write(tmp_path, "cloud: aws\n")
    assert set(refused(storage_env)) == {"STORAGE_BUCKET", "STORAGE_PREFIX"}


def test_a_prefix_without_a_slash_is_refused_when_a_file_is_used(storage_env, tmp_path):
    storage_env["SPECFLOW_CONFIG_FILE"] = write(tmp_path, AWS_FILE.replace("file/", "file"))
    assert refused(storage_env) == ("STORAGE_PREFIX",)


def test_a_deployed_env_refuses_the_local_prefix_with_a_file(storage_env, tmp_path):
    storage_env.update(
        SPECFLOW_CONFIG_FILE=write(tmp_path, AWS_FILE.replace("file/", "dev/local/")),
        APP_ENV="dev",
        AUTH_MODE="oidc",
        OIDC_ISSUER="https://issuer.example.test",
        AUTH_DEV_SIGNING_KEY="",
    )
    storage_env.pop("AUTH_DEV_SIGNING_KEY")
    assert "STORAGE_PREFIX" in refused(storage_env)


def test_the_error_message_has_names_and_no_values(storage_env, tmp_path):
    storage_env.update(
        SPECFLOW_CONFIG_FILE=write(tmp_path, AWS_FILE), STORAGE_BACKEND="gcs", STORAGE_BUCKET="secret-bucket-xyz"
    )
    with pytest.raises(ConfigurationError) as caught:
        load_settings("api", storage_env)
    assert "secret-bucket-xyz" not in str(caught.value) and "file-bucket" not in str(caught.value)


# --- the shipped files -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "cloud", "backend"),
    [
        ("config/cloud.local.yaml", "local", "s3"),
        ("config/examples/cloud.gcp.yaml", "gcp", "gcs"),
        ("config/examples/cloud.aws.yaml", "aws", "s3"),
    ],
)
def test_each_shipped_profile_validates(storage_env, name, cloud, backend):
    storage_env["SPECFLOW_CONFIG_FILE"] = str(ROOT / name)
    found = storage(storage_env)
    assert (found.cloud, found.backend) == (cloud, backend)


def test_the_local_profile_values():
    profile, problems = load_profile(str(ROOT / "config/cloud.local.yaml"))
    assert problems == [] and profile is not None
    assert profile.values == {
        "STORAGE_BACKEND": "s3",
        "STORAGE_BUCKET": "specflow-local",
        "STORAGE_PREFIX": "dev/local/",
        "AWS_ENDPOINT_URL_S3": "http://rustfs:9000",
        "AWS_REGION": "us-east-1",
        "STORAGE_S3_ADDRESSING": "path",
    }
