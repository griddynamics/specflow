"""Self-test for `schema`: the example file loads, derived views are right, bad input refuses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cypher_checks.schema import GraphSchema, SchemaError, load_schema, parse_schema


def test_the_example_schema_declares_its_labels_and_types(schema: GraphSchema) -> None:
    assert set(schema.nodes) == {"Person", "Movie", "Genre"}
    assert set(schema.relationships) == {"ACTED_IN", "DIRECTED", "IN_GENRE", "FOLLOWS"}
    assert schema.relationships["ACTED_IN"].start_labels == ("Person",)
    assert schema.relationships["ACTED_IN"].end_labels == ("Movie",)


def test_common_properties_join_every_label(schema: GraphSchema) -> None:
    assert schema.node_property_names("Genre") == {"id", "status", "name"}
    assert schema.node_property_names("NotALabel") is None


def test_anchorable_properties_are_unique_and_range_indexed(schema: GraphSchema) -> None:
    assert schema.unique_properties == {"id", "name"}
    assert schema.range_indexed_properties() == [("Movie", "title"), ("Person", "name")]
    assert schema.anchorable_properties == {"id", "name", "title"}


def test_vector_properties_are_found_by_type(schema: GraphSchema) -> None:
    by_label = schema.vector_properties_by_label()
    assert by_label["Person"] == {"embedding"}
    assert by_label["Movie"] == {"plot_embedding"}
    assert by_label["Genre"] == frozenset()


def test_a_vector_property_is_never_range_indexed() -> None:
    data: dict[str, Any] = {
        "nodes": {"Doc": {"properties": {"v": {"type": "vector"}, "k": {"type": "string"}}}},
        "constraints": {"indexes": ["Doc.v", "Doc.k", "Doc.undeclared"]},
    }
    assert parse_schema(data).range_indexed_properties() == [("Doc", "k")]


def test_policy_registers_bind_by_file_stem(schema: GraphSchema) -> None:
    policy = schema.policy
    capped = policy.capped_for(Path("q/follower_network.cypher"))
    assert capped is not None
    assert (capped.hard_cap, capped.key) == (4, "FOLLOW_MAX_DEPTH")
    assert policy.capped_for(Path("q/other.cypher")) is None
    assert policy.is_scan_shaped("q/all_genres.cypher")
    assert policy.is_vector_read("q/movie_embeddings.cypher")
    assert policy.removable("q/remove_genre.cypher") == ("Genre", "IN_GENRE")
    assert policy.removable("q/other.cypher") is None


def test_an_empty_policy_fails_closed() -> None:
    plain = parse_schema({"nodes": {"A": {}}})
    assert plain.policy.capped_traversals == ()
    assert not plain.policy.is_scan_shaped("a.cypher")
    assert plain.policy.removable("a.cypher") is None
    assert plain.policy.placeholder_labels == {}


def test_placeholder_labels_map_a_declared_label_or_type_to_concrete_labels() -> None:
    parsed = parse_schema(
        {
            "nodes": {"A": {}, "__ANY__": {}},
            "relationships": {"__REL__": {"from": ["A"], "to": ["A"]}},
            "policy": {"placeholder_labels": {"__ANY__": [], "__REL__": ["LINKS", "USES"]}},
        }
    )
    assert parsed.policy.placeholder_labels == {"__ANY__": (), "__REL__": ("LINKS", "USES")}


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"nodes": {"Not Safe": {}}},
        {"nodes": {"A": {}}, "relationships": {"R": {"from": ["A"]}}},
        {"nodes": {"A": {}}, "constraints": {"unique": ["A_without_property"]}},
        {"nodes": {"A": {}}, "policy": {"capped_traversals": [{"query": "q"}]}},
        {"nodes": {"A": {}}, "policy": {"write_globs": "not-a-list"}},
        {"nodes": {"A": {}}, "policy": {"placeholder_labels": ["A"]}},
        {"nodes": {"A": {}}, "policy": {"placeholder_labels": {"A": "B"}}},
        {"nodes": {"A": {}}, "policy": {"placeholder_labels": {"A": ["Not Safe"]}}},
        {"nodes": {"A": {}}, "policy": {"placeholder_labels": {"__UNDECLARED__": []}}},
    ],
)
def test_malformed_schema_is_refused(data: dict[str, Any]) -> None:
    with pytest.raises(SchemaError):
        parse_schema(data)


def test_an_unreadable_file_is_a_schema_error(tmp_path: Path) -> None:
    with pytest.raises(SchemaError):
        load_schema(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(SchemaError):
        load_schema(bad)
