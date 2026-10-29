"""Self-test for the static lint: every rule caught once, every allowance and example clean.

Path-dependent rules read `policy.write_globs`/`ddl_globs`, so each case is written under
`tmp_path` at the relative path it names (`read/...`, `write/...`)."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from cypher_checks import cyver_check, explain, lint, no_inline_queries, param_names, parity
from cypher_checks.findings import Finding
from cypher_checks.schema import GraphSchema
from tests.conftest import EXAMPLES, KIT


def _lint(tmp_path: Path, schema: GraphSchema, rel: str, text: str) -> list[Finding]:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return lint.check([path], schema, root=tmp_path)


_CAPPED = "read/follower_network.cypher"
_ANCHOR = "MATCH p = (a:Person:Entity {id: $id})"

#: (case id, relative path, query text, the rule it must raise)
NEGATIVE = [
    (
        "header",
        "read/q.cypher",
        "MATCH (m:Movie {title: $t})\nRETURN m.id AS id\n",
        "cypher-lint-missing-header-comment",
    ),
    (
        "two-statements",
        "read/q.cypher",
        "// two\nMATCH (m:Movie {title: $a})\nRETURN m.id AS id;\n"
        "MATCH (m:Movie {title: $b})\nRETURN m.id AS id\n",
        "cypher-lint-multiple-statements",
    ),
    (
        "unbounded",
        _CAPPED,
        f"// walk\n{_ANCHOR}-[:FOLLOWS*]->(b:Person)\n"
        "WHERE length(p) <= $follow_max_depth\nRETURN b.id AS id\n",
        "cypher-lint-unbounded-path",
    ),
    (
        "over-cap",
        _CAPPED,
        f"// walk\n{_ANCHOR}-[:FOLLOWS*1..9]->(b:Person)\n"
        "WHERE length(p) <= $follow_max_depth\nRETURN b.id AS id\n",
        "cypher-lint-hard-cap-exceeded",
    ),
    (
        "no-length-filter",
        _CAPPED,
        f"// walk\n{_ANCHOR}-[:FOLLOWS*1..3]->(b:Person)\nRETURN b.id AS id\n",
        "cypher-lint-missing-length-filter",
    ),
    (
        "wrong-key",
        _CAPPED,
        f"// walk\n{_ANCHOR}-[:FOLLOWS*1..3]->(b:Person)\n"
        "WHERE length(p) <= $other_depth\nRETURN b.id AS id\n",
        "cypher-lint-wrong-config-key",
    ),
    (
        "unregistered-walk",
        "read/friends_of_friends.cypher",
        f"// walk\n{_ANCHOR}-[:FOLLOWS*1..2]->(b:Person)\n"
        "WHERE length(p) <= $max_depth\nRETURN b.id AS id\n",
        "cypher-lint-unregistered-capped-query",
    ),
    (
        "merge-no-key",
        "write/w.cypher",
        "// m\nMERGE (g:Genre)\nRETURN g.name AS name\n",
        "cypher-lint-merge-no-key",
    ),
    (
        "merge-two-keys",
        "write/w.cypher",
        "// m\nMERGE (p:Person:Entity {id: $id, name: $name})\nRETURN p.id AS id\n",
        "cypher-lint-merge-multiple-keys",
    ),
    (
        "merge-literal-key",
        "write/w.cypher",
        "// m\nMERGE (p:Person:Entity {id: 'p-1'})\nRETURN p.id AS id\n",
        "cypher-lint-merge-key-not-parameter",
    ),
    (
        "create",
        "write/add_person.cypher",
        "// c\nCREATE (p:Person:Entity {id: $id})\nRETURN p.id AS id\n",
        "cypher-lint-create-not-allowed",
    ),
    (
        "delete",
        "write/drop_person.cypher",
        "// d\nMATCH (p:Person:Entity {id: $id})\nDETACH DELETE p\n",
        "cypher-lint-delete-not-allowed",
    ),
    (
        "delete-outside-row",
        "write/remove_genre.cypher",
        "// d\nMATCH (p:Person:Entity {id: $id})\nDETACH DELETE p\n",
        "cypher-lint-delete-not-allowed",
    ),
    (
        "write-in-read",
        "read/rename_person.cypher",
        "// w\nMATCH (p:Person:Entity {id: $id})\nSET p.name = $name\nRETURN p.id AS id\n",
        "cypher-lint-write-outside-write-folder",
    ),
    (
        "not-anchored",
        "read/people.cypher",
        "// s\nMATCH (p:Person)\nRETURN p.name AS name\n",
        "cypher-lint-not-anchored",
    ),
    (
        "unknown-type",
        "read/q.cypher",
        "// t\nMATCH (p:Person:Entity {id: $id})-[:ACTS_IN]->(m:Movie)\nRETURN m.id AS id\n",
        "cypher-lint-unknown-relationship-type",
    ),
    (
        "alternation-unknown-type",
        "read/q.cypher",
        "// t\nMATCH (p:Person:Entity {id: $id})-[:ACTED_IN|NOT_A_REL]->(m:Movie)\n"
        "RETURN m.id AS id\n",
        "cypher-lint-unknown-relationship-type",
    ),
    (
        "dynamic-label",
        "read/q.cypher",
        "// t\nMATCH (n:$label {id: $id})\nRETURN n.id AS id\n",
        "cypher-lint-templating-marker",
    ),
    (
        "template",
        "read/q.cypher",
        "// t\nMATCH (m:Movie {title: {{title}}})\nRETURN m.id AS id\n",
        "cypher-lint-templating-marker",
    ),
    (
        "lowercase-keyword",
        "read/q.cypher",
        "// k\nmatch (m:Movie {title: $t})\nRETURN m.id AS id\n",
        "cypher-lint-keyword-not-uppercase",
    ),
    (
        "two-match-one-line",
        "read/q.cypher",
        "// k\nMATCH (m:Movie {title: $t}) MATCH (g:Genre {name: $g})\n"
        "RETURN m.id AS id, g.name AS name\n",
        "cypher-lint-multiple-match-per-line",
    ),
    (
        "qpp-uncommented",
        _CAPPED,
        "// header\nWITH $id AS pid\n"
        "MATCH p = (a:Person:Entity {id: pid}) ((:Person)-[:FOLLOWS]->(:Person)){1,3}\n"
        "WHERE length(p) <= $follow_max_depth\nRETURN length(p) AS hops\n",
        "cypher-lint-qpp-uncommented",
    ),
    (
        "set-unknown",
        "write/w.cypher",
        "// s\nMERGE (p:Person:Entity {id: $id})\nSET p.nickname = $nickname\n",
        "cypher-lint-set-unknown-property",
    ),
    (
        "set-map-unknown",
        "write/w.cypher",
        "// s\nMERGE (p:Person:Entity {id: $id})\nSET p += {nickname: $nickname}\n",
        "cypher-lint-set-unknown-property",
    ),
    (
        "inline-map-unknown",
        "read/q.cypher",
        "// s\nMATCH (m:Movie {rating: $rating})\nRETURN m.id AS id\n",
        "cypher-lint-set-unknown-property",
    ),
    (
        "rel-map-unknown",
        "write/w.cypher",
        "// s\nMATCH (p:Person:Entity {id: $p}), (m:Movie:Entity {id: $m})\n"
        "MERGE (p)-[r:ACTED_IN {billing: $billing}]->(m)\n",
        "cypher-lint-set-unknown-property",
    ),
    (
        "shared-set",
        "write/w.cypher",
        "// s\nMERGE (p:Person:Entity {id: $id})\n"
        "ON CREATE SET p.name = $name, p.status = 'ACTIVE'\nON MATCH SET p.name = $name\n",
        "cypher-lint-shared-set-in-both-branches",
    ),
    (
        "vector-property",
        "read/q.cypher",
        "// v\nMATCH (m:Movie:Entity {id: $id})\nRETURN m.plot_embedding AS v\n",
        "cypher-lint-unregistered-vector-read",
    ),
    (
        "vector-whole-node",
        "read/q.cypher",
        "// v\nMATCH (m:Movie:Entity {id: $id})\nRETURN m\n",
        "cypher-lint-unregistered-vector-read",
    ),
    (
        "vector-star-projection",
        "read/q.cypher",
        "// v\nMATCH (m:Movie:Entity {id: $id})\nRETURN m {.*} AS movie\n",
        "cypher-lint-unregistered-vector-read",
    ),
    (
        "limit-without-order",
        "read/q.cypher",
        "// o\nMATCH (g:Genre {name: $name})<-[:IN_GENRE]-(m:Movie)\nRETURN m.id AS id\nLIMIT 5\n",
        "cypher-lint-limit-without-order",
    ),
    (
        "order-not-total",
        "read/q.cypher",
        "// o\nMATCH (g:Genre {name: $name})<-[:IN_GENRE]-(m:Movie)\n"
        "RETURN m.id AS id, m.released AS released\nORDER BY released DESC\nLIMIT 5\n",
        "cypher-lint-order-not-total",
    ),
]


@pytest.mark.parametrize(
    ("rel", "text", "rule"), [case[1:] for case in NEGATIVE], ids=[case[0] for case in NEGATIVE]
)
def test_negative_case_is_caught(
    tmp_path: Path, schema: GraphSchema, rel: str, text: str, rule: str
) -> None:
    findings = _lint(tmp_path, schema, rel, text)
    assert rule in {f.rule for f in findings}, [f.render() for f in findings]


#: (case id, relative path, query text) -- shapes a rule must not misread.
POSITIVE = [
    (
        "on-create-is-not-create",
        "write/w.cypher",
        "// u\nMERGE (p:Person:Entity {id: $id})\n"
        "ON CREATE SET p.born = $born\nSET p.name = $name\n",
    ),
    (
        "unwind-row-key",
        "write/w.cypher",
        "// b\nUNWIND $rows AS row\nMERGE (p:Person:Entity {id: row.id})\nSET p.name = row.name\n",
    ),
    ("unlabelled-seek", "read/q.cypher", "// s\nMATCH (n {id: $id})\nRETURN n.id AS id\n"),
    (
        "where-seek",
        "read/q.cypher",
        "// s\nMATCH (p:Person)\nWHERE p.name IN $names\nRETURN p.id AS id\n",
    ),
    (
        "later-node-seek",
        "read/q.cypher",
        "// s\nMATCH (p:Person)-[:ACTED_IN]->(m:Movie {title: $title})\nRETURN p.id AS id\n",
    ),
    (
        "keyword-in-string",
        "read/q.cypher",
        "// s\nMATCH (m:Movie {title: $t})\nWHERE m.tagline <> 'match merge: create'\n"
        "RETURN m.id AS id\n",
    ),
    (
        "arrow-is-not-a-comment",
        "read/q.cypher",
        "// s\nMATCH (m:Movie {title: $t})-->(g:Genre)\nRETURN g.name AS name\n",
    ),
    (
        "vector-reduced",
        "read/q.cypher",
        "// v\nMATCH (m:Movie:Entity {id: $id})\n"
        "RETURN size(m.plot_embedding) AS dims, m.plot_embedding IS NOT NULL AS has_vector\n",
    ),
    (
        "star-projection-nulls-vectors",
        "read/q.cypher",
        "// v\nMATCH (m:Movie:Entity {id: $id})\nRETURN m {.*, plot_embedding: null} AS movie\n",
    ),
    (
        "order-by-every-column",
        "read/q.cypher",
        "// o\nMATCH (g:Genre {name: $name})<-[:IN_GENRE]-(m:Movie)\n"
        "RETURN m.released AS released\nORDER BY released\nLIMIT 5\n",
    ),
    (
        "relationship-delete",
        "write/unlink.cypher",
        "// d\nMATCH (p:Person:Entity {id: $id})-[r:FOLLOWS]->(:Person)\nDELETE r\n",
    ),
]


@pytest.mark.parametrize(
    ("rel", "text"), [case[1:] for case in POSITIVE], ids=[case[0] for case in POSITIVE]
)
def test_positive_case_is_clean(tmp_path: Path, schema: GraphSchema, rel: str, text: str) -> None:
    findings = _lint(tmp_path, schema, rel, text)
    assert findings == [], [f.render() for f in findings]


def test_the_examples_are_clean(schema: GraphSchema) -> None:
    findings = lint.check([EXAMPLES / "queries", EXAMPLES / "schema"], schema, root=EXAMPLES)
    assert findings == [], [f.render() for f in findings]


def test_a_registered_repoint_is_the_only_create_allowance(
    tmp_path: Path, schema: GraphSchema
) -> None:
    text = (EXAMPLES / "queries" / "write" / "repoint_acted_in.cypher").read_text()
    assert _lint(tmp_path, schema, "write/repoint_acted_in.cypher", text) == []
    moved = _lint(tmp_path, schema, "write/copy_acted_in.cypher", text)
    assert "cypher-lint-create-not-allowed" in {f.rule for f in moved}


def test_ddl_is_exempt_from_the_data_rules(tmp_path: Path, schema: GraphSchema) -> None:
    text = (
        "// constraints\n"
        "CREATE CONSTRAINT a IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE;\n"
        "CREATE INDEX b IF NOT EXISTS FOR (p:Person) ON (p.name);\n"
    )
    assert _lint(tmp_path, schema, "schema/s.cypher", text) == []
    elsewhere = {f.rule for f in _lint(tmp_path, schema, "write/s.cypher", text)}
    assert {"cypher-lint-create-not-allowed", "cypher-lint-multiple-statements"} <= elsewhere


def test_no_write_globs_turns_the_write_folder_rule_off(
    tmp_path: Path, schema: GraphSchema
) -> None:
    open_policy = replace(schema, policy=replace(schema.policy, write_globs=()))
    text = "// w\nMATCH (p:Person:Entity {id: $id})\nSET p.name = $name\nRETURN p.id AS id\n"
    findings = _lint(tmp_path, open_policy, "read/rename_person.cypher", text)
    assert "cypher-lint-write-outside-write-folder" not in {f.rule for f in findings}


def test_findings_point_at_the_offending_line(tmp_path: Path, schema: GraphSchema) -> None:
    text = (
        "// t\nMATCH (p:Person:Entity {id: $id})\n  -[:ACTED_IN|NOT_A_REL]->(m:Movie)\nRETURN 1\n"
    )
    finding = next(
        f
        for f in _lint(tmp_path, schema, "read/q.cypher", text)
        if f.rule == "cypher-lint-unknown-relationship-type"
    )
    assert (finding.line, finding.col) == (3, 15)
    assert finding.path == "read/q.cypher"


class TestBlanking:
    def test_line_comment_is_blanked_line_numbers_preserved(self) -> None:
        code = "MATCH (n:Movie {id: $id}) // trailing note\nRETURN n\n"
        blanked = lint._blank_comments(code)
        assert "trailing note" not in blanked
        assert blanked.count("\n") == code.count("\n")

    def test_block_comment_spanning_lines_keeps_line_numbers(self) -> None:
        code = "MATCH (n:Movie {id: $id}) /* a\nmultiline\ncomment */\nRETURN n\n"
        blanked = lint._blank_comments(code)
        assert "multiline" not in blanked
        assert blanked.splitlines()[-1] == "RETURN n"

    def test_dash_arrow_is_not_a_comment(self) -> None:
        code = "MATCH (a:Movie {id: $id})-->(b:Genre)\nRETURN b\n"
        assert lint._blank_comments(code) == code

    def test_quoted_strings_keep_quotes_and_length(self) -> None:
        code = "MATCH (n:Movie) WHERE n.title = 'this MERGE thing' RETURN n\n"
        blanked = lint._blank_single_quoted_strings(code)
        assert "MERGE" not in blanked
        assert len(blanked) == len(code)

    def test_doubled_quote_is_an_escape(self) -> None:
        code = "WHERE n.name = 'O''Brien' RETURN n"
        blanked = lint._blank_single_quoted_strings(code)
        assert "Brien" not in blanked
        assert blanked.endswith("' RETURN n")


def _slug(heading: str) -> str:
    text = heading.strip().lstrip("#").strip().replace("`", "").lower()
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", text))


def test_every_cited_doc_anchor_exists() -> None:
    """Every `doc:` a finding renders names a heading of the kit's RULES.md."""
    rules = (KIT / "RULES.md").read_text(encoding="utf-8")
    slugs = {_slug(line) for line in rules.splitlines() if line.startswith("#")}
    cited = {*lint.DOCS, explain.DOC_ANCHOR, explain.CAP_DOC_ANCHOR, cyver_check.DOC_ANCHOR}
    cited |= {no_inline_queries.DOC, param_names.DOC, parity.DOC}
    assert {doc.split("#", 1)[0] for doc in cited} == {"RULES.md"}
    missing = sorted(doc for doc in cited if doc.split("#", 1)[1] not in slugs)
    assert missing == []


def _rules(findings: list[Finding]) -> set[str]:
    return {f.rule for f in findings}


def test_a_write_file_outside_the_allowed_locations_is_flagged(
    tmp_path: Path, schema: GraphSchema
) -> None:
    policy = replace(schema.policy, write_allowed_globs=("**/people/write/*.cypher",))
    scoped = replace(schema, policy=policy)
    text = (EXAMPLES / "queries" / "write" / "merge_person.cypher").read_text(encoding="utf-8")
    allowed = _lint(tmp_path, scoped, "people/write/merge_person.cypher", text)
    elsewhere = _lint(tmp_path, scoped, "orders/write/merge_person.cypher", text)
    assert "cypher-lint-write-outside-capability" not in _rules(allowed)
    assert "cypher-lint-write-outside-capability" in _rules(elsewhere)


def test_every_read_needs_an_owner_and_every_owner_a_read(
    tmp_path: Path, schema: GraphSchema
) -> None:
    policy = replace(
        schema.policy,
        read_globs=("**/read/*.cypher",),
        read_owners=("Movie by title", "Retired query"),
    )
    owned = replace(schema, policy=policy)
    read = tmp_path / "read"
    read.mkdir()
    for stem in ("movie_by_title", "all_genres"):
        source = EXAMPLES / "queries" / "read" / f"{stem}.cypher"
        (read / f"{stem}.cypher").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    findings = lint.check([read], owned, root=tmp_path)
    assert {(f.path, f.rule) for f in findings} == {
        ("read/all_genres.cypher", "cypher-lint-read-without-owner"),
        ("policy.read_owners", "cypher-lint-owner-row-without-read"),
    }


def test_no_read_globs_turns_the_owner_rules_off(tmp_path: Path, schema: GraphSchema) -> None:
    text = (EXAMPLES / "queries" / "read" / "movie_by_title.cypher").read_text(encoding="utf-8")
    assert _lint(tmp_path, schema, "read/movie_by_title.cypher", text) == []


_W = "write/w.cypher"
_R = "read/q.cypher"
_P = "MATCH (p:Person:Entity {id: $id})\n"
_G = "MATCH (g:Genre {name: $name})<-[:IN_GENRE]-(m:Movie)\n"
_WALK = "WHERE length(p) <= $follow_max_depth\nRETURN b.id AS id\n"

#: More regression shapes, grouped by rule. (case id, relative path, query text, rule)
NEGATIVE_MORE = [
    # MERGE key label and key shape
    (
        "merge-label-without-constraint",
        _W,
        "// m\nMERGE (p:Person {id: $id})\nRETURN p.id AS id\n",
        "cypher-lint-merge-key-label-unconstrained",
    ),
    (
        "merge-key-on-unconstrained-property",
        _W,
        "// m\nMERGE (p:Person:Entity {name: $name})\nRETURN p.id AS id\n",
        "cypher-lint-merge-key-label-unconstrained",
    ),
    (
        "merge-unwind-rebound-key",
        _W,
        "// m\nUNWIND $rows AS row\nWITH row.id AS rid\nMERGE (p:Person:Entity {id: rid})\n"
        "SET p.name = $name\n",
        "cypher-lint-merge-key-not-parameter",
    ),
    (
        "merge-unwind-derived-key",
        _W,
        "// m\nUNWIND $rows AS row\nMERGE (p:Person:Entity {id: toLower(row.id)})\n"
        "SET p.name = row.name\n",
        "cypher-lint-merge-key-not-parameter",
    ),
    (
        "merge-map-spans-lines",
        _W,
        "// m\nMERGE (p:Person:Entity {\n  id: $id,\n  name: $name\n})\nRETURN p.id AS id\n",
        "cypher-lint-merge-multiple-keys",
    ),
    (
        "merge-empty-map",
        _W,
        "// m\nMERGE (p:Person:Entity {})\nRETURN p.id AS id\n",
        "cypher-lint-merge-no-key",
    ),
    (
        "merge-nested-map-top-level-key",
        _W,
        "// m\nMERGE (p:Person:Entity {id: $id, born: datetime({year: 1999})})\n",
        "cypher-lint-merge-multiple-keys",
    ),
    (
        "two-merges-one-line-second-has-no-key",
        _W,
        "// m\nMERGE (p:Person:Entity {id: $id}) MERGE (g:Genre)\nRETURN p.id AS id\n",
        "cypher-lint-merge-no-key",
    ),
    # CREATE / DELETE
    (
        "with-create-mid-line",
        _W,
        f"// c\n{_P}WITH p CREATE (q:Person:Entity {{id: $other}})\nRETURN q.id AS id\n",
        "cypher-lint-create-not-allowed",
    ),
    (
        "detach-delete-of-a-relationship-variable",
        "write/drop_link.cypher",
        "// d\nMATCH (p:Person:Entity {id: $id})-[r:FOLLOWS]->(:Person)\nDETACH DELETE r\n",
        "cypher-lint-delete-not-allowed",
    ),
    (
        "delete-of-an-unbound-variable",
        "write/drop_link.cypher",
        f"// d\n{_P}DELETE q\n",
        "cypher-lint-delete-not-allowed",
    ),
    (
        "delete-in-an-unregistered-file",
        "write/remove_movie.cypher",
        "// d\nMATCH (g:Genre {name: $name})\nDETACH DELETE g\n",
        "cypher-lint-delete-not-allowed",
    ),
    # bounds
    (
        "open-upper-bound",
        _CAPPED,
        f"// walk\n{_ANCHOR}-[:FOLLOWS*2..]->(b:Person)\n{_WALK}",
        "cypher-lint-unbounded-path",
    ),
    (
        "qpp-plus-is-unbounded",
        _CAPPED,
        "// header\nMATCH p = (a:Person:Entity {id: $id}) ((:Person)-[:FOLLOWS]->(:Person))+\n"
        f"(b:Person)\n{_WALK}",
        "cypher-lint-unbounded-path",
    ),
    (
        "qpp-star-is-unbounded",
        _CAPPED,
        "// header\nMATCH p = (a:Person:Entity {id: $id}) ((:Person)-[:FOLLOWS]->(:Person))*\n"
        f"(b:Person)\n{_WALK}",
        "cypher-lint-unbounded-path",
    ),
    # anchors
    (
        "where-on-another-variable",
        _R,
        "// s\nMATCH (p:Person)-[:ACTED_IN]->(m:Movie)\nWHERE m.title = $t\nRETURN p.id AS id\n",
        "cypher-lint-not-anchored",
    ),
    (
        "contains-does-not-anchor",
        _R,
        "// s\nMATCH (p:Person)\nWHERE p.name CONTAINS $s\nRETURN p.id AS id\n",
        "cypher-lint-not-anchored",
    ),
    (
        "first-unanchored-later-anchored",
        _R,
        "// s\nMATCH (p:Person)\nMATCH (g:Genre {name: $name})\nRETURN p.id AS id\n",
        "cypher-lint-not-anchored",
    ),
    (
        "union-part-unanchored",
        _R,
        "// s\nMATCH (g:Genre {name: $a})\nRETURN g.name AS name\nUNION\n"
        "MATCH (p:Person)\nRETURN p.name AS name\n",
        "cypher-lint-not-anchored",
    ),
    (
        "unlabelled-seek-on-unindexed-property",
        _R,
        "// s\nMATCH (n {status: $s})\nRETURN n.id AS id\n",
        "cypher-lint-not-anchored",
    ),
    (
        "unlabelled-seek-on-a-literal",
        _R,
        "// s\nMATCH (n {id: 'p-1'})\nRETURN n.id AS id\n",
        "cypher-lint-not-anchored",
    ),
    (
        "unlabelled-where-on-unindexed-property",
        _R,
        "// s\nMATCH (n)\nWHERE n.status = $s\nRETURN n.id AS id\n",
        "cypher-lint-not-anchored",
    ),
    # relationship vocabulary and templating
    (
        "repeated-colon-alternation",
        _R,
        f"// t\n{_P.rstrip()}-[:ACTED_IN|:NOT_A_REL]->(m:Movie)\nRETURN m.id AS id\n",
        "cypher-lint-unknown-relationship-type",
    ),
    (
        "unknown-type-with-a-map",
        _R,
        f"// t\n{_P.rstrip()}-[:ACTS_IN {{since: 1}}]->(m:Movie)\nRETURN m.id AS id\n",
        "cypher-lint-unknown-relationship-type",
    ),
    (
        "unknown-type-with-a-quantifier",
        _R,
        f"// t\n{_P.rstrip()}-[:NOT_A_REL*1..2]->(m:Movie)\nRETURN m.id AS id\n",
        "cypher-lint-unknown-relationship-type",
    ),
    (
        "apoc-cypher-run",
        _R,
        f"// t\n{_P}CALL apoc.cypher.run('RETURN 1', {{}}) YIELD value\nRETURN value\n",
        "cypher-lint-templating-marker",
    ),
    (
        "format-placeholder",
        _R,
        "// t\nMATCH (m:Movie {title: %s})\nRETURN m.id AS id\n",
        "cypher-lint-templating-marker",
    ),
    # formatting
    (
        "qpp-comment-below-the-clause-does-not-count",
        _CAPPED,
        "MATCH p = (a:Person:Entity {id: $id}) ((:Person)-[:FOLLOWS]->(:Person)){1,3}\n"
        f"// late\n(b:Person)\n{_WALK}",
        "cypher-lint-qpp-uncommented",
    ),
    (
        "qpp-dash-arrow-is-not-a-comment",
        _CAPPED,
        "MATCH p = (a:Person:Entity {id: $id})-->(:Person)\n"
        f"((:Person)-[:FOLLOWS]->(:Person)){{1,3}}\n{_WALK}",
        "cypher-lint-qpp-uncommented",
    ),
    # SET / map properties
    (
        "relationship-dot-set",
        _W,
        "// s\nMATCH (p:Person:Entity {id: $id})-[r:FOLLOWS]->(:Person)\nSET r.weight = $w\n",
        "cypher-lint-set-unknown-property",
    ),
    (
        "nested-map-does-not-hide-a-top-level-key",
        _W,
        "// s\nMERGE (p:Person:Entity {id: $id})\n"
        "SET p += {nickname: $nick, born: datetime({year: 1999})}\n",
        "cypher-lint-set-unknown-property",
    ),
    (
        "create-map-unknown-with-a-nested-constructor",
        "write/add_person.cypher",
        "// c\nCREATE (p:Person:Entity {id: $id, nickname: datetime({year: 1999})})\n",
        "cypher-lint-set-unknown-property",
    ),
    (
        "shared-set-two-assignments",
        _W,
        "// s\nMERGE (p:Person:Entity {id: $id})\n"
        "ON CREATE SET p.name = $name, p.born = $born\n"
        "ON MATCH SET p.born = $born, p.name = $name\n",
        "cypher-lint-shared-set-in-both-branches",
    ),
    (
        "shared-set-with-a-comma-in-a-call",
        _W,
        "// s\nMERGE (p:Person:Entity {id: $id})\n"
        "ON CREATE SET p.name = coalesce($a, $b)\nON MATCH SET p.name = coalesce($a, $b)\n",
        "cypher-lint-shared-set-in-both-branches",
    ),
    (
        "shared-set-second-merge-after-a-clean-one",
        _W,
        "// s\nMERGE (g:Genre {name: $g})\nON CREATE SET g.status = 'ACTIVE'\n"
        "ON MATCH SET g.status = 'INACTIVE'\n"
        "MERGE (p:Person:Entity {id: $id})\n"
        "ON CREATE SET p.name = $name\nON MATCH SET p.name = $name\n",
        "cypher-lint-shared-set-in-both-branches",
    ),
    # vectors
    (
        "vector-shorthand-projection",
        _R,
        "// v\nMATCH (m:Movie:Entity {id: $id})\nRETURN m {.title, .plot_embedding} AS movie\n",
        "cypher-lint-unregistered-vector-read",
    ),
    (
        "vector-node-through-a-subquery-return",
        _R,
        "// v\nMATCH (m:Movie:Entity {id: $id})\nCALL (m) {\n  RETURN m AS node\n}\nRETURN node\n",
        "cypher-lint-unregistered-vector-read",
    ),
    (
        "vector-in-a-union-part",
        _R,
        "// v\nMATCH (m:Movie:Entity {id: $a})\nRETURN m.id AS x\nUNION\n"
        "MATCH (n:Movie:Entity {id: $b})\nRETURN n.plot_embedding AS x\n",
        "cypher-lint-unregistered-vector-read",
    ),
    (
        "vector-conjunction-with-a-vector-free-label",
        _R,
        "// v\nMATCH (m:Movie&Entity {id: $id})\nRETURN m\n",
        "cypher-lint-unregistered-vector-read",
    ),
    (
        "vector-as-a-list-element",
        _R,
        "// v\nMATCH (m:Movie:Entity {id: $id})\nRETURN [m.title, m.plot_embedding] AS row\n",
        "cypher-lint-unregistered-vector-read",
    ),
    (
        "vector-through-an-alias-rebinding",
        _R,
        "// v\nMATCH (m:Movie:Entity {id: $id})\nWITH m AS film\nRETURN film.plot_embedding AS v\n",
        "cypher-lint-unregistered-vector-read",
    ),
    (
        "vector-through-a-collected-node-list",
        _R,
        "// v\nMATCH (m:Movie:Entity {id: $id})\nRETURN collect(m) AS movies\n",
        "cypher-lint-unregistered-vector-read",
    ),
    # deterministic cuts
    (
        "order-by-in-an-earlier-projection-only",
        _R,
        f"// o\n{_G}WITH m ORDER BY m.id\nRETURN m.id AS id\nLIMIT 5\n",
        "cypher-lint-limit-without-order",
    ),
    (
        "order-not-total-before-skip-and-limit",
        _R,
        f"// o\n{_G}RETURN m.id AS id, m.released AS released\n"
        "ORDER BY released DESC\nSKIP 5\nLIMIT 5\n",
        "cypher-lint-order-not-total",
    ),
    (
        "order-unique-only-as-an-alias",
        _R,
        f"// o\n{_G}RETURN m.released AS id, m.title AS title\nORDER BY id\nLIMIT 5\n",
        "cypher-lint-order-not-total",
    ),
    (
        "starts-with-is-not-a-projection",
        _R,
        f"// o\n{_G}WHERE m.title STARTS WITH $p\nRETURN m.id AS id\nLIMIT 5\n",
        "cypher-lint-limit-without-order",
    ),
    (
        "unordered-cut-inside-a-subquery",
        _R,
        "// o\nMATCH (g:Genre {name: $name})\nCALL (g) {\n  MATCH (g)<-[:IN_GENRE]-(m:Movie)\n"
        "  RETURN m.id AS id\n  LIMIT 5\n}\nRETURN id\nORDER BY id\n",
        "cypher-lint-limit-without-order",
    ),
]


@pytest.mark.parametrize(
    ("rel", "text", "rule"),
    [case[1:] for case in NEGATIVE_MORE],
    ids=[case[0] for case in NEGATIVE_MORE],
)
def test_more_negative_cases_are_caught(
    tmp_path: Path, schema: GraphSchema, rel: str, text: str, rule: str
) -> None:
    findings = _lint(tmp_path, schema, rel, text)
    assert rule in _rules(findings), [f.render() for f in findings]


#: More shapes a rule must not misread. (case id, relative path, query text)
POSITIVE_MORE = [
    ("merge-label-pair", _W, "// m\nMERGE (p:Person:Entity {id: $id})\nSET p.name = $name\n"),
    ("merge-label-after-the-constrained-one", _W, "// m\nMERGE (e:Entity:Person {id: $id})\n"),
    ("merge-on-a-constrained-non-common-label", _W, "// m\nMERGE (g:Genre {name: $name})\n"),
    (
        "merge-key-map-spans-lines",
        _W,
        "// m\nMERGE (p:Person:Entity {\n  id: $id\n})\nSET p.name = $name\n",
    ),
    (
        "merge-colon-in-a-double-quoted-value",
        _W,
        '// m\nMERGE (p:Person:Entity {id: $id})\nSET p += {name: "a: b"}\n',
    ),
    (
        "merge-nested-constructor-in-set",
        _W,
        "// m\nMERGE (p:Person:Entity {id: $id})\nSET p += {born: datetime({year: 1999}).year}\n",
    ),
    (
        "merge-relationship-between-bound-nodes",
        _W,
        "// m\nMATCH (p:Person:Entity {id: $p}), (m:Movie:Entity {id: $m})\n"
        "MERGE (p)-[:ACTED_IN]->(m)\n",
    ),
    (
        "created-at-continuation-line",
        _R,
        "// s\nMATCH (m:Movie {title: $t})\nWITH m, m.released AS created_year\n"
        "RETURN m.id AS id,\n  created_year\n",
    ),
    (
        "create-keyword-inside-a-string",
        _R,
        "// s\nMATCH (m:Movie {title: $t})\nWHERE m.tagline = 'create or delete'\n"
        "RETURN m.id AS id\n",
    ),
    (
        "labelled-seek-with-a-label-pair",
        _R,
        "// s\nMATCH (p:Person:Entity {id: $id})\nRETURN p.id AS id\n",
    ),
    ("range-indexed-unlabelled-seek", _R, "// s\nMATCH (n {title: $t})\nRETURN n.id AS id\n"),
    (
        "equality-where-anchor",
        _R,
        "// s\nMATCH (p:Person)\nWHERE p.name = $name\nRETURN p.id AS id\n",
    ),
    (
        "every-union-part-anchored",
        _R,
        "// s\nMATCH (g:Genre {name: $a})\nRETURN g.name AS name\nUNION\n"
        "MATCH (p:Person {name: $b})\nRETURN p.name AS name\n",
    ),
    (
        "repeated-colon-alternation",
        _R,
        f"// t\n{_P.rstrip()}-[:ACTED_IN|:DIRECTED]->(m:Movie)\nRETURN m.id AS id\n",
    ),
    (
        "type-name-inside-a-string",
        _R,
        "// t\nMATCH (m:Movie {title: $t})\nWHERE m.tagline = '[:NOT_A_REL]'\nRETURN m.id AS id\n",
    ),
    (
        "type-and-quantifier-known",
        _CAPPED,
        f"// walk\n{_ANCHOR}-[:FOLLOWS*1..3]->(b:Person)\n{_WALK}",
    ),
    (
        "bare-upper-bound-is-bounded",
        _CAPPED,
        f"// walk\n{_ANCHOR}-[:FOLLOWS*3]->(b:Person)\n{_WALK}",
    ),
    (
        "lower-omitted-upper-bound-is-bounded",
        _CAPPED,
        f"// walk\n{_ANCHOR}-[:FOLLOWS*..3]->(b:Person)\n{_WALK}",
    ),
    (
        "qpp-with-a-block-comment-above",
        _CAPPED,
        "/* the walk repeats one hop */\nMATCH p = (a:Person:Entity {id: $id})\n"
        f"((:Person)-[:FOLLOWS]->(:Person)){{1,3}}\n(b:Person)\n{_WALK}",
    ),
    (
        "qpp-with-a-multi-line-comment-inside-the-pattern",
        _CAPPED,
        "// header\nWITH $id AS pid\nMATCH p = (a:Person:Entity {id: pid})\n"
        "/* one hop,\n   repeated */\n"
        f"((:Person)-[:FOLLOWS]->(:Person)){{1,3}}\n(b:Person)\n{_WALK}",
    ),
    (
        "set-on-an-unbound-variable",
        _W,
        f"// s\n{_P}SET q.nickname = $nick\n",
    ),
    (
        "set-nested-constructor-map",
        _W,
        "// s\nMERGE (p:Person:Entity {id: $id})\nSET p.born = datetime({year: 1999}).year\n",
    ),
    (
        "shared-set-different-values",
        _W,
        "// s\nMERGE (p:Person:Entity {id: $id})\nON CREATE SET p.status = 'ACTIVE'\n"
        "ON MATCH SET p.status = 'INACTIVE'\n",
    ),
    (
        "shared-set-once-per-merge",
        _W,
        "// s\nMERGE (p:Person:Entity {id: $id})\nON CREATE SET p.status = 'ACTIVE'\n"
        "MERGE (q:Person:Entity {id: $other})\nMERGE (p)-[:FOLLOWS]->(q)\n"
        "ON MATCH SET p.status = 'ACTIVE'\n",
    ),
    (
        "vector-subquery-reduced",
        _R,
        "// v\nMATCH (m:Movie:Entity {id: $id})\n"
        "CALL (m) {\n  RETURN size(m.plot_embedding) AS dims\n}\nRETURN dims\n",
    ),
    (
        "vector-free-label",
        _R,
        "// v\nMATCH (g:Genre {name: $name})\nRETURN g\n",
    ),
    (
        "vector-value-not-a-node",
        _R,
        "// v\nMATCH (m:Movie:Entity {id: $id})\nWITH m.title AS t\nRETURN t\n",
    ),
    (
        "order-ending-in-a-unique-property",
        _R,
        f"// o\n{_G}RETURN m.id AS id, m.released AS released\n"
        "ORDER BY released DESC, id\nLIMIT 5\n",
    ),
    (
        "order-with-skip-between-order-and-limit",
        _R,
        f"// o\n{_G}RETURN m.id AS id\nORDER BY id\nSKIP 5\nLIMIT 5\n",
    ),
    (
        "order-by-in-the-cut-projection-after-a-with-order",
        _R,
        f"// o\n{_G}WITH m ORDER BY m.id\nRETURN m.id AS id\nORDER BY id\nLIMIT 5\n",
    ),
    (
        "starts-with-then-ordered-limit",
        _R,
        f"// o\n{_G}RETURN m.id AS id\nORDER BY m.title STARTS WITH $p DESC, id\nLIMIT 5\n",
    ),
    (
        "limit-parameter-on-ordered-unique",
        _R,
        f"// o\n{_G}RETURN m.id AS id\nORDER BY id\nLIMIT $n\n",
    ),
    (
        "in-list-literal-anchor",
        _R,
        "// s\nMATCH (p:Person)\nWHERE p.name IN ['a', 'b']\nRETURN p.id AS id\n",
    ),
    (
        "ordered-cut-inside-a-subquery",
        _R,
        "// o\nMATCH (g:Genre {name: $name})\nCALL (g) {\n  MATCH (g)<-[:IN_GENRE]-(m:Movie)\n"
        "  RETURN m.id AS id\n  ORDER BY id\n  LIMIT 5\n}\nRETURN id\n",
    ),
]


@pytest.mark.parametrize(
    ("rel", "text"),
    [case[1:] for case in POSITIVE_MORE],
    ids=[case[0] for case in POSITIVE_MORE],
)
def test_more_positive_cases_are_clean(
    tmp_path: Path, schema: GraphSchema, rel: str, text: str
) -> None:
    findings = _lint(tmp_path, schema, rel, text)
    assert findings == [], [f.render() for f in findings]


def test_node_removals_listing_only_a_relationship_type_allow_no_node_delete(
    tmp_path: Path, schema: GraphSchema
) -> None:
    text = (EXAMPLES / "queries" / "write" / "remove_genre.cypher").read_text(encoding="utf-8")
    rel = "write/remove_genre.cypher"
    assert "cypher-lint-delete-not-allowed" not in _rules(_lint(tmp_path, schema, rel, text))
    narrowed = replace(
        schema, policy=replace(schema.policy, node_removals={"Remove genre": ("IN_GENRE",)})
    )
    assert "cypher-lint-delete-not-allowed" in _rules(_lint(tmp_path, narrowed, rel, text))


def test_only_read_files_need_an_owner(tmp_path: Path, schema: GraphSchema) -> None:
    owned = replace(
        schema, policy=replace(schema.policy, read_globs=("**/read/*.cypher",), read_owners=())
    )
    text = "// w\nMERGE (g:Genre {name: $name})\nRETURN g.name AS name\n"
    assert "cypher-lint-read-without-owner" not in _rules(
        _lint(tmp_path, owned, "write/merge_genre.cypher", text)
    )
    assert "cypher-lint-read-without-owner" in _rules(
        _lint(
            tmp_path,
            owned,
            "read/some_read.cypher",
            "// r\nMATCH (g:Genre {name: $n})\nRETURN g.name AS n\n",
        )
    )


def test_a_merge_key_label_rule_is_off_without_unique_constraints(
    tmp_path: Path, schema: GraphSchema
) -> None:
    bare = replace(schema, unique=())
    text = "// m\nMERGE (p:Person {id: $id})\nRETURN p.id AS id\n"
    assert "cypher-lint-merge-key-label-unconstrained" not in _rules(
        _lint(tmp_path, bare, _W, text)
    )


def test_a_merge_in_a_ddl_file_is_not_checked_for_its_key_label(
    tmp_path: Path, schema: GraphSchema
) -> None:
    text = "// m\nMERGE (p:Person {id: $id})\n"
    assert "cypher-lint-merge-key-label-unconstrained" not in _rules(
        _lint(tmp_path, schema, "schema/s.cypher", text)
    )


def test_merge_scope_keys_sit_beside_the_one_key_as_parameters(
    tmp_path: Path, schema: GraphSchema
) -> None:
    scoped = replace(schema, policy=replace(schema.policy, merge_scope_keys=("tenant",)))
    text = "// m\nMERGE (p:Person:Entity {tenant: $tenant, id: $id})\nRETURN p.id AS id\n"
    assert "cypher-lint-merge-multiple-keys" in _rules(_lint(tmp_path, schema, _W, text))
    assert _rules(_lint(tmp_path, scoped, _W, text)).isdisjoint(
        {
            "cypher-lint-merge-multiple-keys",
            "cypher-lint-merge-key-not-parameter",
            "cypher-lint-merge-key-label-unconstrained",
        }
    )
    literal = "// m\nMERGE (p:Person:Entity {tenant: 't', id: $id})\nRETURN p.id AS id\n"
    assert "cypher-lint-merge-key-not-parameter" in _rules(_lint(tmp_path, scoped, _W, literal))
    two = "// m\nMERGE (p:Person:Entity {tenant: $t, id: $id, name: $n})\nRETURN p.id AS id\n"
    assert "cypher-lint-merge-multiple-keys" in _rules(_lint(tmp_path, scoped, _W, two))


def test_a_batch_merge_query_keys_on_a_row_of_an_earlier_unwind(
    tmp_path: Path, schema: GraphSchema
) -> None:
    rel = "write/upsert_people.cypher"
    batch = replace(schema, policy=replace(schema.policy, batch_merge_queries=("upsert_people",)))
    later = (
        "// m\nUNWIND $rows AS row\nMATCH (g:Genre {name: $genre})\n"
        "MERGE (p:Person:Entity {id: row.id})\nSET p.name = row.name\n"
    )
    nested = (
        "// m\nUNWIND $rows AS row\nMATCH (g:Genre {name: $genre})\nCALL (row) {\n"
        "UNWIND row.people AS person\nMERGE (p:Person:Entity {id: person.id})\n}\n"
    )
    rebound = (
        "// m\nUNWIND $rows AS row\nWITH row.first AS row\n"
        "MERGE (p:Person:Entity {id: row.id})\nSET p.name = row.name\n"
    )
    derived = (
        "// m\nUNWIND $rows AS row\nMATCH (g:Genre {name: $genre})\n"
        "MERGE (p:Person:Entity {id: toLower(row.id)})\n"
    )
    rule = "cypher-lint-merge-key-not-parameter"
    assert rule in _rules(_lint(tmp_path, schema, rel, later))
    assert rule not in _rules(_lint(tmp_path, batch, rel, later))
    assert rule not in _rules(_lint(tmp_path, batch, rel, nested))
    assert rule in _rules(_lint(tmp_path, batch, rel, rebound))
    assert rule in _rules(_lint(tmp_path, batch, rel, derived))


def test_a_registered_repoint_may_move_every_edge_it_matches_by_pattern(
    tmp_path: Path, schema: GraphSchema
) -> None:
    text = (
        "// Move every ACTED_IN edge of the merged person onto the survivor.\n"
        "MATCH (old:Person:Entity {id: $old_id})-[r:ACTED_IN]->(m:Movie)\n"
        "MATCH (survivor:Person:Entity {id: $survivor_id})\n"
        "CREATE (survivor)-[moved:ACTED_IN]->(m)\n"
        "SET moved = properties(r)\n"
        "DELETE r\n"
    )
    rule = "cypher-lint-create-not-allowed"
    assert rule not in _rules(_lint(tmp_path, schema, "write/repoint_all_acted_in.cypher", text))
    assert rule in _rules(_lint(tmp_path, schema, "write/copy_all_acted_in.cypher", text))
    kept = text.replace("DELETE r\n", "RETURN moved\n")
    assert rule in _rules(_lint(tmp_path, schema, "write/repoint_kept.cypher", kept))
    node = text.replace(
        "CREATE (survivor)-[moved:ACTED_IN]->(m)", "CREATE (q:Person:Entity {id: $id})"
    )
    assert rule in _rules(_lint(tmp_path, schema, "write/repoint_node.cypher", node))
