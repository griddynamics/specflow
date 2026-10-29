"""The cloud profile file: which cloud runs the app and which service each capability uses.

The file is YAML. Its path comes from ``SPECFLOW_CONFIG_FILE``; unset or blank means
no file. This module reads and checks the file only. ``settings`` merges the values
with the environment (the environment wins) and applies the guards.
Secrets never belong in the file: an unknown key is an error, not a secret store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

CONFIG_FILE_VARIABLE: Final = "SPECFLOW_CONFIG_FILE"

Cloud = Literal["local", "gcp", "aws"]
Provider = Literal["gcs", "s3"]

# The providers each cloud allows; the first one is the default.
CLOUD_PROVIDERS: Final[dict[str, tuple[Provider, ...]]] = {
    "gcp": ("gcs",),
    "aws": ("s3",),
    "local": ("s3",),
}

# A file key (dotted path under `services.object_storage`) -> the environment variable that overrides it.
_KEY_TO_VARIABLE: Final = {
    "provider": "STORAGE_BACKEND",
    "bucket": "STORAGE_BUCKET",
    "prefix": "STORAGE_PREFIX",
    "s3.endpoint_url": "AWS_ENDPOINT_URL_S3",
    "s3.region": "AWS_REGION",
    "s3.addressing": "STORAGE_S3_ADDRESSING",
    "gcs.project": "GOOGLE_CLOUD_PROJECT",
}
_OBJECT_STORAGE_PATH: Final = ("services", "object_storage")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _S3(_Strict):
    endpoint_url: str | None = None
    region: str | None = None
    addressing: Literal["auto", "path"] | None = None


class _Gcs(_Strict):
    project: str | None = None


class _ObjectStorage(_Strict):
    provider: Provider | None = None
    bucket: str | None = None
    prefix: str | None = None
    s3: _S3 = _S3()
    gcs: _Gcs = _Gcs()


class _Services(_Strict):
    object_storage: _ObjectStorage = _ObjectStorage()


class _ProfileFile(BaseSettings):
    """Only the schema; the YAML source below fills it. Nothing here reads the process environment."""

    model_config = SettingsConfigDict(extra="forbid")

    cloud: Cloud
    services: _Services = _Services()


@dataclass(frozen=True)
class CloudProfile:
    """A checked profile: the cloud and the object-storage values, keyed by their env variable."""

    cloud: Cloud
    values: dict[str, str] = field(default_factory=dict)


def _variable_for(location: tuple[int | str, ...], kind: str) -> str:
    """The name a validation error reports: an env variable, ``cloud``, or the dotted key."""

    dotted = ".".join(str(part) for part in location)
    if kind == "extra_forbidden":
        return dotted
    if location == ("cloud",):
        return "cloud"
    if location[: len(_OBJECT_STORAGE_PATH)] == _OBJECT_STORAGE_PATH:
        key = ".".join(str(part) for part in location[len(_OBJECT_STORAGE_PATH) :])
        return _KEY_TO_VARIABLE.get(key, dotted)
    return dotted


def _flatten(profile: _ProfileFile) -> dict[str, str]:
    store = profile.services.object_storage
    found: dict[str, Any] = {
        "provider": store.provider or CLOUD_PROVIDERS[profile.cloud][0],
        "bucket": store.bucket,
        "prefix": store.prefix,
        "s3.endpoint_url": store.s3.endpoint_url,
        "s3.region": store.s3.region,
        "s3.addressing": store.s3.addressing,
        "gcs.project": store.gcs.project,
    }
    return {_KEY_TO_VARIABLE[key]: value for key, value in found.items() if value is not None}


def load_profile(path: str | None) -> tuple[CloudProfile | None, list[str]]:
    """Read and check the profile file.

    Returns ``(profile, [])`` on success, ``(None, [])`` when no file is set, and
    ``(None, names)`` on a refusal. ``names`` are variable names or dotted keys, never values.
    """

    if path is None or not path.strip():
        return None, []
    file = Path(path.strip())
    try:
        if not file.is_file():
            return None, [CONFIG_FILE_VARIABLE]
        data = YamlConfigSettingsSource(_ProfileFile, yaml_file=file)()
    except (OSError, yaml.YAMLError, ValueError, TypeError, AttributeError):
        # Unreadable file, invalid YAML or not a mapping. The cause can carry file text, so it is dropped.
        return None, [CONFIG_FILE_VARIABLE]
    try:
        parsed = _ProfileFile.model_validate(data)
    except ValidationError as error:
        names: list[str] = []
        for item in error.errors(include_url=False, include_input=False):
            name = _variable_for(tuple(item["loc"]), item["type"])
            if name not in names:
                names.append(name)
        return None, names
    return CloudProfile(cloud=parsed.cloud, values=_flatten(parsed)), []
