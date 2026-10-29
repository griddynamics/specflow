from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError
from sqlalchemy import URL, make_url

from settings import (
    DATABASE_ENVIRONMENT,
    DatabaseConfigurationError,
    DatabaseSettings,
)

# Written out literally, not read from settings, so a change to the
# code under test cannot shrink the test matrix along with it. The names are
# the environment contract of the deployment.
DATABASE_KEYS = (
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)

# Every key but the port must be set; the port defaults to 5432.
REQUIRED_KEYS = tuple(name for name in DATABASE_KEYS if name != "DATABASE_PORT")

# The pre-contract names. Cleared too, so a developer's old .env cannot make
# a test pass by accident.
LEGACY_KEYS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)

# Dummy credentials. CANARY occurs in no other value, so "absent from the
# output" cannot be a false positive (or a false negative).
ENVIRONMENT = {
    "DATABASE_HOST": "localhost",
    "DATABASE_PORT": "5433",
    "DATABASE_NAME": "d",
    "DATABASE_USER": "u",
    "DATABASE_PASSWORD": "p",
}
CANARY = "s3cr3t!"
SPECIAL_CRED = "p@ss:w/rd%"

Load = Callable[..., DatabaseSettings]


@pytest.fixture(autouse=True)
def _clean_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*DATABASE_KEYS, *LEGACY_KEYS):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def load(monkeypatch: pytest.MonkeyPatch) -> Load:
    """Set ENVIRONMENT plus overrides (None unsets), then read the settings."""

    def apply(**overrides: str | None) -> DatabaseSettings:
        for name, value in {**ENVIRONMENT, **overrides}.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return DatabaseSettings.from_environment()

    return apply


# T1 ------------------------------------------------------------------------


def test_every_value_comes_from_its_database_variable(load: Load) -> None:
    resolved = load()

    assert resolved.host == "localhost"
    assert resolved.port == 5433
    assert resolved.name == "d"
    assert resolved.user == "u"
    assert resolved.password.get_secret_value() == "p"


def test_overrides_are_applied(load: Load) -> None:
    resolved = load(DATABASE_HOST="db.local", DATABASE_PORT="6543")

    assert (resolved.host, resolved.port) == ("db.local", 6543)


def test_text_values_are_trimmed(load: Load) -> None:
    resolved = load(DATABASE_HOST=" h ", DATABASE_NAME=" d ", DATABASE_USER=" u ")

    assert (resolved.host, resolved.name, resolved.user) == ("h", "d", "u")


def test_the_settings_are_frozen(load: Load) -> None:
    with pytest.raises(ValidationError):
        load().port = 1


# T2 ------------------------------------------------------------------------


def test_the_variable_names_match_the_environment_contract() -> None:
    assert DATABASE_ENVIRONMENT == DATABASE_KEYS


def test_an_empty_environment_names_every_variable() -> None:
    with pytest.raises(DatabaseConfigurationError) as caught:
        DatabaseSettings.from_environment()

    message = str(caught.value)
    for name in REQUIRED_KEYS:
        assert name in message
    assert "DATABASE_PORT" not in message
    assert "--env-file ../.env.local" in message


def test_the_legacy_postgres_variables_are_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in LEGACY_KEYS:
        monkeypatch.setenv(name, "x")

    with pytest.raises(DatabaseConfigurationError) as caught:
        DatabaseSettings.from_environment()

    for name in REQUIRED_KEYS:
        assert name in str(caught.value)


@pytest.mark.parametrize("blank", ["", "   ", None], ids=["empty", "spaces", "unset"])
@pytest.mark.parametrize("blanked", DATABASE_KEYS)
def test_a_blank_variable_is_reported_and_the_others_are_not(load: Load, blanked: str, blank: str | None) -> None:
    if blanked == "DATABASE_PORT" and blank is None:
        assert load(**{blanked: None}).port == 5432  # unset port: the default
        return
    with pytest.raises(DatabaseConfigurationError) as caught:
        load(**{blanked: blank})

    message = str(caught.value)
    assert blanked in message
    for name in DATABASE_KEYS:
        if name != blanked:
            assert name not in message


def test_every_unusable_variable_is_listed_together(load: Load) -> None:
    with pytest.raises(DatabaseConfigurationError) as caught:
        load(DATABASE_USER="  ", DATABASE_PASSWORD=None, DATABASE_PORT="abc")

    message = str(caught.value)
    for name in ("DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_PORT"):
        assert name in message
    for name in ("DATABASE_HOST", "DATABASE_NAME"):
        assert name not in message


@pytest.mark.parametrize("field", ["host", "name", "user", "password"])
@pytest.mark.parametrize("blank", ["", "  "], ids=["empty", "spaces"])
def test_direct_construction_rejects_a_blank_field(field: str, blank: str) -> None:
    values = {"host": "h", "port": 5432, "name": "d", "user": "u", "password": "p"}
    values[field] = blank

    with pytest.raises(ValidationError, match=field):
        DatabaseSettings(**values)


# T3 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("port", "reason"),
    [
        ("abc", "valid integer"),
        ("0", "greater than or equal to 1"),
        ("65536", "less than or equal to 65535"),
    ],
)
def test_an_unusable_port_is_rejected(load: Load, port: str, reason: str) -> None:
    with pytest.raises(DatabaseConfigurationError, match=f"DATABASE_PORT: .*{reason}"):
        load(DATABASE_PORT=port)


@pytest.mark.parametrize("port", [1, 65535])
def test_the_port_boundaries_are_accepted(load: Load, port: int) -> None:
    assert load(DATABASE_PORT=str(port)).port == port


# T4 ------------------------------------------------------------------------


@pytest.mark.parametrize("port", ["abc", "0", "65536"])
def test_the_password_is_absent_from_a_configuration_error(load: Load, port: str) -> None:
    with pytest.raises(DatabaseConfigurationError) as caught:
        load(DATABASE_PASSWORD=CANARY, DATABASE_PORT=port)

    assert CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value)


def test_the_password_is_absent_from_the_missing_variable_error(load: Load) -> None:
    with pytest.raises(DatabaseConfigurationError) as caught:
        load(DATABASE_PASSWORD=CANARY, DATABASE_USER="")

    assert CANARY not in str(caught.value)


def test_the_password_is_absent_from_the_repr(load: Load) -> None:
    resolved = load(DATABASE_PASSWORD=CANARY)

    assert resolved.password.get_secret_value() == CANARY
    assert CANARY not in repr(resolved)
    assert CANARY not in str(resolved)


def test_the_url_uses_asyncpg_and_keeps_a_special_password_intact(
    load: Load,
) -> None:
    url = load(DATABASE_PASSWORD=SPECIAL_CRED).url()

    assert isinstance(url, URL)
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "u"
    assert url.host == "localhost"
    assert url.port == 5433
    assert url.database == "d"
    assert url.password == SPECIAL_CRED


def test_the_url_escapes_special_characters_and_round_trips(load: Load) -> None:
    url = load(DATABASE_PASSWORD=SPECIAL_CRED).url()

    rendered = url.render_as_string(hide_password=False)

    assert SPECIAL_CRED not in rendered
    assert "p%40ss%3Aw%2Frd%25" in rendered
    assert make_url(rendered).password == SPECIAL_CRED


def test_the_url_masks_the_password_when_rendered(load: Load) -> None:
    url = load(DATABASE_PASSWORD=SPECIAL_CRED).url()

    for text in (url.render_as_string(), str(url), repr(url)):
        assert SPECIAL_CRED not in text
        assert "p%40ss" not in text
        assert ":***@" in text
