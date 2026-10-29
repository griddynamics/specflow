"""`fixture_graph` unit tests: no live database, grounded in the example schema."""

from __future__ import annotations

import pytest

from cypher_checks import fixture_graph
from cypher_checks.schema import GraphSchema


class TestDummyValue:
    def test_enum_prefers_the_first_declared_value(self) -> None:
        assert fixture_graph.dummy_value("status", "string", ("ACTIVE", "INACTIVE")) == "ACTIVE"

    @pytest.mark.parametrize(
        ("type_text", "expected"),
        [
            ("integer", 1),
            ("float", 1.0),
            ("boolean", True),
            ("timestamp", "2024-01-01T00:00:00Z"),
            ("json", "{}"),
            ("vector", [1.0]),
            ("list<integer>", [1]),
            ("list of string", ["fixture"]),
            ("string", "fixture"),
            ("", "fixture"),
        ],
    )
    def test_type_text_picks_the_dummy(self, type_text: str, expected: object) -> None:
        assert fixture_graph.dummy_value("prop", type_text) == expected

    def test_id_type_is_qualified_by_the_property_name(self) -> None:
        assert fixture_graph.dummy_value("owner_id", "id") == "owner_id-fixture"


class TestNodeFixtures:
    def test_one_fixture_per_declared_label(self, schema: GraphSchema) -> None:
        fixtures = fixture_graph.build_node_fixtures(schema)
        assert {f.label for f in fixtures} == set(schema.nodes)

    def test_every_common_and_own_property_is_present(self, schema: GraphSchema) -> None:
        person = next(f for f in fixture_graph.build_node_fixtures(schema) if f.label == "Person")
        assert set(person.properties) == schema.node_property_names("Person")
        assert person.properties["status"] == "ACTIVE"

    def test_a_property_unique_on_the_common_label_is_label_qualified(
        self, schema: GraphSchema
    ) -> None:
        ids = {f.properties["id"] for f in fixture_graph.build_node_fixtures(schema)}
        assert ids == {"Person-id-fixture", "Movie-id-fixture", "Genre-id-fixture"}


class TestRelFixtures:
    def test_every_relationship_type_is_represented(self, schema: GraphSchema) -> None:
        fixtures = fixture_graph.build_rel_fixtures(schema)
        assert {f.rel_type for f in fixtures} == set(schema.relationships)

    def test_each_edge_uses_its_declared_direction(self, schema: GraphSchema) -> None:
        edges = {
            (f.rel_type, f.start_label, f.end_label)
            for f in fixture_graph.build_rel_fixtures(schema)
        }
        assert ("ACTED_IN", "Person", "Movie") in edges
        assert ("IN_GENRE", "Movie", "Genre") in edges
        assert ("ACTED_IN", "Movie", "Person") not in edges

    def test_relationship_properties_get_the_generic_dummy(self, schema: GraphSchema) -> None:
        acted = next(
            f for f in fixture_graph.build_rel_fixtures(schema) if f.rel_type == "ACTED_IN"
        )
        assert acted.properties == {"roles": "fixture"}


class TestIdentifierSafety:
    def test_rejects_a_label_that_is_not_a_safe_identifier(self) -> None:
        with pytest.raises(fixture_graph.FixtureGraphError):
            fixture_graph.assert_identifier("Not Safe", "node label")

    def test_accepts_a_plain_identifier(self) -> None:
        fixture_graph.assert_identifier("Person", "node label")
