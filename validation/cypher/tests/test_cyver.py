"""CyVer detection proved against a real Neo4j testcontainer seeded from the example schema.

Each construct is caught in each clause shape; known misses (`fixtures/cyver/known_gaps/`)
are proved missed, and covered by another check (`test_explain.py`). Integration: needs
Docker."""

from __future__ import annotations

from pathlib import Path

import neo4j
import pytest

from cypher_checks import cyver_check
from cypher_checks.schema import GraphSchema
from tests.conftest import EXAMPLES, FIXTURES

pytestmark = pytest.mark.integration

POSITIVE_DIR = FIXTURES / "cyver" / "positive"
NEGATIVE_DIR = FIXTURES / "cyver" / "negative"
KNOWN_GAPS_DIR = FIXTURES / "cyver" / "known_gaps"

_UNKNOWN_TYPE = "Neo.ClientNotification.Statement.UnknownRelationshipTypeWarning"
_UNKNOWN_LABEL = "Neo.ClientNotification.Statement.UnknownLabelWarning"
_UNKNOWN_PROPERTY = "Neo.ClientNotification.Statement.UnknownPropertyKeyWarning"

# Every negative fixture's stem -> the one code its self-test requires present (a floor),
# so each construct's detection is proved, not just some finding.
_REQUIRED_CODE = {
    "alternation_one_unknown_relationship_type": _UNKNOWN_TYPE,
    "match_conflicting_labels": "ConflictingVariablesError",
    "match_syntax_error": "Neo.ClientError.Statement.SyntaxError",
    "match_unknown_label": _UNKNOWN_LABEL,
    "match_unknown_property": _UNKNOWN_PROPERTY,
    "match_unknown_relationship_type": _UNKNOWN_TYPE,
    "match_wrong_direction": "WrongDirectionPathWarning",
    "merge_unknown_label": _UNKNOWN_LABEL,
    "merge_unknown_relationship_type": _UNKNOWN_TYPE,
    "optional_match_unknown_label": _UNKNOWN_LABEL,
    "qpp_unknown_relationship_type": _UNKNOWN_TYPE,
    "set_unknown_property": _UNKNOWN_PROPERTY,
}


def test_negative_fixture_stems_match_the_required_code_map() -> None:
    assert {p.stem for p in NEGATIVE_DIR.glob("*.cypher")} == set(_REQUIRED_CODE)


@pytest.mark.parametrize("path", sorted(POSITIVE_DIR.glob("*.cypher")), ids=lambda p: p.stem)
def test_positive_fixture_is_clean(neo4j_driver: neo4j.Driver, path: Path) -> None:
    findings = cyver_check.check_query(neo4j_driver, str(path), path.read_text(encoding="utf-8"))
    assert findings == [], [f.render() for f in findings]


@pytest.mark.parametrize("path", sorted(NEGATIVE_DIR.glob("*.cypher")), ids=lambda p: p.stem)
def test_negative_fixture_is_caught(neo4j_driver: neo4j.Driver, path: Path) -> None:
    findings = cyver_check.check_query(neo4j_driver, str(path), path.read_text(encoding="utf-8"))
    assert _REQUIRED_CODE[path.stem] in {f.rule for f in findings}, [f.render() for f in findings]
    for finding in findings:
        rendered = finding.render()
        assert cyver_check.DOC_ANCHOR in rendered
        assert rendered.count("fix: ") == 1, rendered


@pytest.mark.parametrize("path", sorted(KNOWN_GAPS_DIR.glob("*.cypher")), ids=lambda p: p.stem)
def test_known_gap_is_not_caught_by_cyver_alone(neo4j_driver: neo4j.Driver, path: Path) -> None:
    """Documents exactly what CyVer misses. A CyVer release that starts catching one of these
    fails this test: the signal to move the fixture out of `known_gaps/`."""
    findings = cyver_check.check_query(neo4j_driver, str(path), path.read_text(encoding="utf-8"))
    assert findings == [], [f.render() for f in findings]


def test_the_examples_validate_cleanly(neo4j_driver: neo4j.Driver, schema: GraphSchema) -> None:
    paths = sorted((EXAMPLES / "queries").rglob("*.cypher"))
    paths += sorted((EXAMPLES / "schema").rglob("*.cypher"))
    findings = cyver_check.check_files(neo4j_driver, paths, schema.policy.ddl_globs, EXAMPLES)
    assert findings == [], [f.render() for f in findings]


def test_syntax_notifications_other_than_parameter_not_provided_survive() -> None:
    kept = cyver_check.drop_expected_notifications(
        [
            {"code": cyver_check.PARAM_NOT_PROVIDED, "description": "x"},
            {"code": "Neo.ClientError.Statement.SyntaxError", "description": "y"},
        ]
    )
    assert kept == [{"code": "Neo.ClientError.Statement.SyntaxError", "description": "y"}]
