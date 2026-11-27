"""Offline tier of ``db.dropbox``: the statements of ``ConnectionRows`` (user filter, upsert, no e-mail column, no
rotation) and the transaction ``connection_store`` opens. No database; the live behaviour is the GitHub twin's
(``test_db_github_live``) over the same statement shapes."""

from __future__ import annotations

from typing import Any, cast

from db.dropbox import ConnectionRows, connection_store
from db.models import DropboxConnection
from db_connection_support import LATER, NOW, USER, Session, check_connection_store, check_expire_and_delete
from dropbox.client import DropboxAccount
from dropbox.connections import StoredConnection


def rows(session: Session) -> ConnectionRows:
    return ConnectionRows(cast(Any, session))


def model(**changes: Any) -> DropboxConnection:
    values: dict[str, Any] = {
        "user_id": USER,
        "account_id": "dbid:1",
        "name": "Pat",
        "team_id": "dbtid:5",
        "team_name": "Acme",
        "refresh_token": memoryview(b"cipher"),
        "status": "active",
        "connected_at": NOW,
        "last_used_at": NOW,
    }
    return DropboxConnection(**{**values, **changes})


async def test_get_selects_the_users_row_and_hands_over_ciphertext_bytes() -> None:
    session = Session(model())

    found = await rows(session).get(USER)

    assert found == StoredConnection(USER, "dbid:1", "Pat", "dbtid:5", "Acme", b"cipher", "active", NOW, NOW)
    assert "dropbox_connections.user_id = $1" in session.sql[0] and "FOR UPDATE" not in session.sql[0]
    assert await rows(Session()).get(USER) is None


async def test_a_personal_row_has_no_team() -> None:
    found = await rows(Session(model(team_id=None, team_name=None))).get(USER)

    assert found is not None and (found.team_id, found.team_name) == (None, None)


async def test_save_upserts_on_the_user_and_stores_the_account_without_an_email() -> None:
    session = Session()

    await rows(session).save(USER, DropboxAccount("dbid:1", "Pat", None, None, "99"), refresh_token=b"cipher", now=NOW)

    sql = session.sql[0]
    assert "ON CONFLICT (user_id) DO UPDATE" in sql and "email" not in sql and "refresh_expires_at" not in sql
    assert "root_namespace" not in sql  # the root namespace is read per check and import, never stored (DB6)
    values = " ".join(f"{key}={value!r}" for key, value in session.params[0].items())
    assert "'active'" in values and "b'cipher'" in values and "'Pat'" in values and "'dbid:1'" in values


async def test_touch_expire_and_delete_each_filter_on_the_user() -> None:
    session = Session(model())

    await rows(session).touch(USER, now=LATER)
    await check_expire_and_delete(rows, session, "dropbox_connections")

    assert "last_used_at" in session.sql[0] and "refresh_token" not in session.sql[0]
    assert "status" in session.sql[1] and "expired_at" in session.sql[1] and "RETURNING" in session.sql[2]


async def test_connection_store_yields_the_rows_of_the_transaction_session() -> None:
    await check_connection_store(connection_store, ConnectionRows)
