"""Live gateway smoke over the real LLM gateway endpoint (plan 1.T7, NFR-PORT-01).

Gated by ``SPECFLOW_TEST_LLM_GATEWAY=1``; run with ``uv run --env-file ../.env.local``. One structured
call per chat class with a tiny output model, and one embedding whose vector must have
``GRAPH_EMBEDDING_DIM`` entries and fit the vector indexes: the dimension of the live Neo4j
``graph_spec_node_embedding`` index when ``SPECFLOW_TEST_NEO4J=1`` too, else the data model's 3072.
The API key is never printed or asserted into a message.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import BaseModel

from graph.core import LLMConfig, connect
from graph.core.llm import Gateway
from graph_support import neo4j_config_from_env
from specflow_live import live_enabled, requires_live

pytestmark = requires_live("LLM_GATEWAY")

NEEDED = (
    "LLM_GATEWAY_URL",
    "LLM_GATEWAY_API_KEY",
    "GRAPH_LLM_MODEL",
    "GRAPH_SMALL_MODEL",
    "GRAPH_EMBEDDING_MODEL",
    "GRAPH_EMBEDDING_DIM",
)


DATA_MODEL_EMBEDDING_DIM = 3072
EMBEDDING_INDEX = "graph_spec_node_embedding"


async def vector_index_dimension() -> int:
    """The dimension the embeddings must have: the live index's, or the data model's default."""

    if not live_enabled("NEO4J"):
        return DATA_MODEL_EMBEDDING_DIM
    config = neo4j_config_from_env()
    driver = connect(config)
    try:
        result = await driver.execute_query(
            "SHOW VECTOR INDEXES YIELD name, options WHERE name = $name "
            "RETURN options.indexConfig['vector.dimensions'] AS dimension",
            {"name": EMBEDDING_INDEX},
            database_=config.database,
        )
    finally:
        await driver.close()
    if not result.records:
        pytest.fail(f"no {EMBEDDING_INDEX} index on the live Neo4j: run the live Neo4j tests first")
    return int(result.records[0]["dimension"])


class Greeting(BaseModel):
    word: str
    number: int


def live_config() -> LLMConfig:
    missing = [name for name in NEEDED if not os.environ.get(name, "").strip()]
    if missing:
        pytest.fail(f"live gateway test needs these variables in the environment: {', '.join(missing)}")
    return LLMConfig(
        endpoint=os.environ["LLM_GATEWAY_URL"].strip(),
        api_key=os.environ["LLM_GATEWAY_API_KEY"].strip(),
        main_model=os.environ["GRAPH_LLM_MODEL"].strip(),
        small_model=os.environ["GRAPH_SMALL_MODEL"].strip(),
        embedding_model=os.environ["GRAPH_EMBEDDING_MODEL"].strip(),
        embedding_dim=int(os.environ["GRAPH_EMBEDDING_DIM"]),
    )


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[Gateway]:
    instance = Gateway(live_config())
    try:
        yield instance
    finally:
        await instance.aclose()


@pytest.mark.parametrize("call_class", ["extraction", "small", "judge"])
async def test_structured_call_per_class(gateway: Gateway, call_class: str) -> None:
    result = await gateway.structured(
        call_class,
        None,
        [{"role": "user", "content": "Return the word 'hello' and the number 7."}],
        Greeting,
        prompt=f"live_{call_class}",
    )
    assert isinstance(result, Greeting)
    assert result.word.strip().lower().strip("'\".!") == "hello"
    assert result.number == 7
    counts = gateway.usage().total(call_class=call_class)
    assert counts.calls == 1 and counts.failures == 0
    assert counts.requests >= 1
    assert counts.input_tokens > 0 and counts.output_tokens > 0


async def test_embed_returns_one_vector_of_the_configured_dimension(
    gateway: Gateway,
) -> None:
    vectors = await gateway.embed(["hello"])
    assert len(vectors) == 1
    assert len(vectors[0]) == gateway.config.embedding_dim
    assert len(vectors[0]) == await vector_index_dimension()
    assert all(isinstance(value, float) for value in vectors[0])
    counts = gateway.usage().total(call_class="embedding")
    assert counts.calls == 1 and counts.failures == 0
