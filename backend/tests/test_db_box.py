"""Offline tier of ``db.box``: the statements of ``ConnectionRows`` (user filter, row lock, upsert, no e-mail column)
and the transaction ``connection_store`` opens. No database; the live behaviour is the GitHub twin's
(``test_db_github_live``) over the same statement shapes."""

from __future__ import annotations

from typing import Any, cast

from box.client import BoxUser
from box.connections import StoredConnection
from db.box import ConnectionRows, connection_store
from db.models import BoxConnection
from db_connection_support import LATER, NOW, USER, Session, check_connection_store, check_expire_and_delete


def rows(session: Session) -> ConnectionRows:
    return ConnectionRows(cast(Any, session))


def model(**changes: Any) -> BoxConnection:
    values: dict[str, Any] = {
        "user_id": USER,
        "box_user_id": "11",
        "name": "Pat",
        "enterprise_id": "5",
        "enterprise_name": "Acme",
        "refresh_token": memoryview(b"cipher"),
        "status": "active",
        "connected_at": NOW,
        "last_used_at": NOW,
    }
    return BoxConnection(**{**values, **changes})


async def test_get_selects_the_users_row_and_hands_over_ciphertext_bytes() -> None:
    session = Session(model())

    found = await rows(session).get(USER)

    assert found == StoredConnection(USER, "11", "Pat", "5", "Acme", b"cipher", "active", NOW, NOW)
    assert "box_connections.user_id = $1" in session.sql[0] and "FOR UPDATE" not in session.sql[0]
    assert await rows(Session()).get(USER) is None


async def test_lock_selects_for_update() -> None:
    session = Session(model())

    assert await rows(session).lock(USER) is not None
    assert "FOR UPDATE" in session.sql[0] and "box_connections.user_id = $1" in session.sql[0]


async def test_save_upserts_on_the_user_and_stores_the_account_without_an_email() -> None:
    session = Session()

    await rows(session).save(
        USER, BoxUser("11", "Pat", None, None), refresh_token=b"cipher", refresh_expires_at=LATER, now=NOW
    )

    sql = session.sql[0]
    assert "ON CONFLICT (user_id) DO UPDATE" in sql and "login" not in sql and "email" not in sql
    values = " ".join(f"{key}={value!r}" for key, value in session.params[0].items())
    assert "'active'" in values and "b'cipher'" in values and "'Pat'" in values


async def test_rotate_expire_and_delete_each_filter_on_the_user() -> None:
    session = Session(model())

    await rows(session).rotate(USER, refresh_token=b"new", refresh_expires_at=LATER, now=LATER)
    await check_expire_and_delete(rows, session, "box_connections")

    assert "refresh_token" in session.sql[0] and "status" in session.sql[1] and "RETURNING" in session.sql[2]


async def test_connection_store_yields_the_rows_of_the_transaction_session() -> None:
    await check_connection_store(connection_store, ConnectionRows)
