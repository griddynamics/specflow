"""`EXPLAIN` plan checks against the seeded fixture graph: notifications, `AllNodesScan`,
banned label scans with the scan-shaped exemption, and the registered cap.

Integration: needs Docker."""

from __future__ import annotations

from pathlib import Path

import neo4j
import pytest

from cypher_checks import explain, lint
from cypher_checks.schema import GraphSchema
from tests.conftest import EXAMPLES, FIXTURES

pytestmark = pytest.mark.integration

POSITIVE_DIR = FIXTURES / "explain" / "positive"
NEGATIVE_DIR = FIXTURES / "explain" / "negative"
KNOWN_GAPS_DIR = FIXTURES / "cyver" / "known_gaps"

# Every negative fixture's stem -> the one code its self-test requires present (a floor).
# "01N01" is the GQLSTATUS of a deprecated-function warning, "03N90" of a cartesian product.
_REQUIRED_CODE = {
    "all_nodes_scan": "AllNodesScan",
    "banned_label_scan": "NodeByLabelScan",
    "banned_label_scan_intersection": "NodeByLabelScan",
    "banned_label_scan_union": "NodeByLabelScan",
    "deprecated_function": "01N01",
    "cartesian_product": "03N90",
    "follower_network": "cap-mismatch",
}


def test_negative_fixtures_cover_every_required_code() -> None:
    assert {p.stem for p in NEGATIVE_DIR.glob("*.cypher")} == set(_REQUIRED_CODE)


def _check(driver: neo4j.Driver, schema: GraphSchema, path: Path) -> list[str]:
    findings = explain.check_query(driver, str(path), path.read_text(encoding="utf-8"), schema)
    for finding in findings:
        rendered = finding.render()
        assert rendered.count("fix: ") == 1, rendered
        assert rendered.count("doc:") == 1, rendered
    return [f.rule for f in findings]


@pytest.mark.parametrize("path", sorted(POSITIVE_DIR.glob("*.cypher")), ids=lambda p: p.stem)
def test_positive_fixture_is_clean(
    neo4j_driver: neo4j.Driver, schema: GraphSchema, path: Path
) -> None:
    assert _check(neo4j_driver, schema, path) == []


@pytest.mark.parametrize("path", sorted(NEGATIVE_DIR.glob("*.cypher")), ids=lambda p: p.stem)
def test_negative_fixture_is_caught(
    neo4j_driver: neo4j.Driver, schema: GraphSchema, path: Path
) -> None:
    rules = _check(neo4j_driver, schema, path)
    assert _REQUIRED_CODE[path.stem] in rules, rules


def test_cap_mismatch_names_the_registered_cap_and_key(
    neo4j_driver: neo4j.Driver, schema: GraphSchema
) -> None:
    path = NEGATIVE_DIR / "follower_network.cypher"
    findings = explain.check_query(neo4j_driver, str(path), path.read_text(), schema)
    mismatch = next(f for f in findings if f.rule == "cap-mismatch")
    assert "4" in mismatch.message
    assert "FOLLOW_MAX_DEPTH" in mismatch.message
    assert explain.CAP_DOC_ANCHOR in mismatch.render()


def test_the_examples_plan_cleanly(neo4j_driver: neo4j.Driver, schema: GraphSchema) -> None:
    paths = sorted((EXAMPLES / "queries").rglob("*.cypher"))
    paths += sorted((EXAMPLES / "schema").rglob("*.cypher"))
    findings = explain.check_files(neo4j_driver, paths, schema, root=EXAMPLES)
    assert findings == [], [f.render() for f in findings]


def test_the_plan_check_covers_the_map_projection_gap(
    neo4j_driver: neo4j.Driver, schema: GraphSchema
) -> None:
    """The map-projection key CyVer misses is an unknown-property warning on the plan."""
    path = KNOWN_GAPS_DIR / "map_projection_unknown_property.cypher"
    assert _check(neo4j_driver, schema, path) != []


def test_the_lint_covers_the_set_map_gap(schema: GraphSchema) -> None:
    """The `SET var += {...}` key neither live check sees is the static lint's finding."""
    path = KNOWN_GAPS_DIR / "set_merge_map_unknown_property.cypher"
    rules = {f.rule for f in lint.check([path], schema)}
    assert "cypher-lint-set-unknown-property" in rules
