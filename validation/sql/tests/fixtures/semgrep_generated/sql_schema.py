"""Fixture for the rules `sqlcheck render-semgrep` writes from tests/fixtures/sql_rules/
schema.json: allow-list `cache_entry`, `password_reset`; append-only `audit_log`
(model `AuditLog`) and `login_event` (model `LoginEvent`)."""

# --- kit.sql.delete-outside-allowlist ---


async def delete_order(session: object, order: object) -> None:
    # ruleid: kit.sql.delete-outside-allowlist
    await session.delete(order)


async def delete_cache_entry(session: object, cache_entry: object) -> None:
    # ok: kit.sql.delete-outside-allowlist
    await session.delete(cache_entry)


async def delete_password_reset(db: object, passwordreset: object) -> None:
    # the allow-list matches with or without the underscores
    # ok: kit.sql.delete-outside-allowlist
    await db.delete(passwordreset)


async def delete_account(conn: object, account: object) -> None:
    # ruleid: kit.sql.delete-outside-allowlist
    await conn.delete(account)


async def discard_from_set(tags: set[str], tag: str) -> None:
    # not a session-, db-, connection- or uow-named target
    # ok: kit.sql.delete-outside-allowlist
    tags.delete(tag)


# --- kit.sql.no-mutation-of-append-only-table ---


def build_audit_cleanup_statement() -> object:
    # ruleid: kit.sql.no-mutation-of-append-only-table
    return delete(AuditLog)


def build_login_event_edit_statement(new_kind: str) -> object:
    # ruleid: kit.sql.no-mutation-of-append-only-table
    return update(LoginEvent).values(kind=new_kind)


def build_order_delete_statement() -> object:
    # ok: kit.sql.no-mutation-of-append-only-table
    return delete(CustomerOrder)


def delete(model: type) -> object: ...


def update(model: type) -> object: ...


class AuditLog: ...


class LoginEvent: ...


class CustomerOrder: ...
