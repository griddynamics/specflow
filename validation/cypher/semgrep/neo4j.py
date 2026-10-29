"""Fixtures for neo4j.yml: positive and negative cases per rule, one section per rule id."""


# --- neo4j.autocommit-session-run ---


async def read_names(store: object) -> list[str]:
    async with store.session() as session:
        # ruleid: neo4j.autocommit-session-run
        result = await session.run("MATCH (n) RETURN n.name AS name")
        return [record["name"] async for record in result]


async def read_via_managed_tx(store: object, query: object) -> list[str]:
    async with store.session() as session:
        # ok: neo4j.autocommit-session-run
        return await session.execute_read(run_read, query)


async def run_read(tx: object, query: object) -> list[str]:
    # ok: neo4j.autocommit-session-run
    result = await tx.run(query)
    return [record["name"] async for record in result]


# --- neo4j.loaded-query-mutated ---

_PEOPLE_QUERY_PATH = "people/queries/read/by_name.cypher"
_ORDER_QUERY_PATH = "orders/queries/write/upsert_order.cypher"


def build_scoped_query(region: str) -> str:
    # ruleid: neo4j.loaded-query-mutated
    PEOPLE_BY_NAME = load_named_query(_PEOPLE_QUERY_PATH)
    extra = f" AND n.region = '{region}'"
    return PEOPLE_BY_NAME + extra


def build_write_with_format(label: str) -> str:
    # ruleid: neo4j.loaded-query-mutated
    UPSERT_ORDER = load_cypher(_ORDER_QUERY_PATH)
    return UPSERT_ORDER.format(label=label)


def build_scoped_query_fstring(region: str) -> str:
    # ruleid: neo4j.loaded-query-mutated
    PEOPLE_BY_NAME = load_query(_PEOPLE_QUERY_PATH)
    return f"{PEOPLE_BY_NAME} AND n.region = '{region}'"


def run_with_params(session: object) -> object:
    # ok: neo4j.loaded-query-mutated
    PEOPLE_BY_NAME = load_named_query(_PEOPLE_QUERY_PATH)
    return run_read(session, PEOPLE_BY_NAME, region="north")


def load_query(path: str) -> str: ...


def load_named_query(path: str) -> str: ...


def load_cypher(path: str) -> str: ...


# --- neo4j.driver-constructed-outside-graph-module ---


def build_driver_ad_hoc(uri: str, auth: object) -> object:
    # ruleid: neo4j.driver-constructed-outside-graph-module
    return GraphDatabase.driver(uri, auth=auth)


def build_async_driver_ad_hoc(uri: str, auth: object) -> object:
    # ruleid: neo4j.driver-constructed-outside-graph-module
    return AsyncGraphDatabase.driver(uri, auth=auth)


def reuse_shared_driver(driver: object) -> object:
    # ok: neo4j.driver-constructed-outside-graph-module
    return driver


class GraphDatabase:
    @staticmethod
    def driver(uri: str, auth: object) -> object: ...


class AsyncGraphDatabase:
    @staticmethod
    def driver(uri: str, auth: object) -> object: ...


# --- neo4j.ad-hoc-session-outside-graph-module ---


async def read_scoped(driver: object) -> object:
    # ruleid: neo4j.ad-hoc-session-outside-graph-module
    async with driver.session() as session:
        return await session.execute_read(run_read, "query")


async def read_via_helper(run_read_helper: object) -> object:
    # ok: neo4j.ad-hoc-session-outside-graph-module
    return await run_read_helper("query")
