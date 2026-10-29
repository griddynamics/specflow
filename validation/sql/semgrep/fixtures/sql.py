"""Fixtures for semgrep/sql.yml's rules. Deliberately outside src/services/ and
src/database/ so the rules' `paths.exclude` does not shield this file."""


class OrderRepository:
    def __init__(self, session: object) -> None:
        self._session = session

    async def deactivate(self, order_id: str) -> None:
        await self._session.execute(update_stmt(order_id))
        # ruleid: kit.sql.commit-outside-service
        await self._session.commit()


def update_stmt(order_id: str) -> object: ...


async def deactivate_via_db(db: object, order_id: str) -> None:
    # the common injected-session parameter name is the bare `db`
    # ruleid: kit.sql.commit-outside-service
    await db.commit()
    _ = order_id


async def deactivate_via_underscored_db(_db: object) -> None:
    # ruleid: kit.sql.commit-outside-service
    await _db.commit()


async def deactivate_via_conn(conn: object) -> None:
    # ruleid: kit.sql.commit-outside-service
    await conn.commit()


async def retain_worker_state(session: object) -> None:
    # ruleid: kit.sql.commit-outside-service
    await session.commit()


async def flush_cache(cache: object) -> None:
    # not a session-, db-, connection- or uow-named target
    # ok: kit.sql.commit-outside-service
    await cache.commit()


def build_ad_hoc_engine(url: str) -> object:
    # ruleid: kit.sql.self-opened-engine-outside-database-module
    return create_async_engine(url)


def build_session_factory(engine: object) -> object:
    # ruleid: kit.sql.self-opened-engine-outside-database-module
    return async_sessionmaker(engine)


def build_sync_engine(url: str) -> object:
    # ruleid: kit.sql.self-opened-engine-outside-database-module
    return create_engine(url)


def build_sync_session_factory(engine: object) -> object:
    # ruleid: kit.sql.self-opened-engine-outside-database-module
    return sessionmaker(engine)


def build_scoped(factory: object) -> object:
    # ruleid: kit.sql.self-opened-engine-outside-database-module
    return scoped_session(factory)


def open_session_directly(engine: object) -> object:
    # ruleid: kit.sql.self-opened-engine-outside-database-module
    return Session(engine)


def reuse_injected_session(session: object) -> object:
    # ok: kit.sql.self-opened-engine-outside-database-module
    return session


def create_async_engine(url: str) -> object: ...


def async_sessionmaker(engine: object) -> object: ...


def create_engine(url: str) -> object: ...


def sessionmaker(engine: object) -> object: ...


def scoped_session(factory: object) -> object: ...


def Session(engine: object) -> object: ...  # noqa: N802


# --- kit.sql.no-network-call-in-open-session ---


async def sync_order_status(session: object, url: str) -> None:
    # both rules match the same `async with session.begin():` line: one for
    # the bare `.begin()` call, one for the whole with-block shape
    # ruleid: kit.sql.commit-outside-service, kit.sql.no-network-call-in-open-session
    async with session.begin():
        response = await httpx.get(url)
        _ = response


async def sync_order_status_ok(session: object, response: object) -> None:
    # ruleid: kit.sql.commit-outside-service
    async with session.begin():
        # ok: kit.sql.no-network-call-in-open-session
        record_status(response)


def sync_order_status_blocking(session: object, url: str) -> None:
    # the synchronous `with` + `requests` shape
    # ruleid: kit.sql.commit-outside-service, kit.sql.no-network-call-in-open-session
    with session.begin():
        response = requests.get(url)
        _ = response


def record_status(response: object) -> None: ...


class requests:
    @staticmethod
    def get(url: str) -> object: ...


class httpx:
    @staticmethod
    async def get(url: str) -> object: ...
