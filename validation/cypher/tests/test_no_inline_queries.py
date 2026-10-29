"""Self-test for `no_inline_queries`: query-shaped strings, loader targets and orphans."""

from __future__ import annotations

from pathlib import Path

import pytest

from cypher_checks import no_inline_queries as rule
from tests.conftest import FIXTURES

FIXTURE_DIR = FIXTURES / "no_inline_queries"


def _config(root: Path, **loaders: str) -> rule.Config:
    return rule.Config(root=root, loaders={name: root / base for name, base in loaders.items()})


def test_positive_fixture_is_flagged() -> None:
    config = _config(FIXTURE_DIR, load_query="src")
    findings = rule.scan_file(FIXTURE_DIR / "positive.py", set(), config)
    rules = {f.rule for f in findings}
    assert {
        "no-inline-queries-cypher",
        "no-inline-queries-sql",
        "no-inline-queries-missing-file",
    } <= rules


def test_negative_fixture_is_clean() -> None:
    config = _config(FIXTURE_DIR, load_query="src")
    assert rule.scan_file(FIXTURE_DIR / "negative.py", set(), config) == []


def test_fstring_with_cypher_text_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "m.py"
    path.write_text('q = f"MATCH (n:Person {{id: $id}}) RETURN n"\n')
    findings = rule.scan_file(path, set(), _config(tmp_path))
    assert [f.rule for f in findings] == ["no-inline-queries-cypher"]


def test_loader_call_resolving_to_a_real_file_is_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    query = tmp_path / "src" / "x.cypher"
    query.write_text("MATCH (n) RETURN n\n")
    module = tmp_path / "m.py"
    module.write_text('load_query("x.cypher")\n')
    referenced: set[Path] = set()
    assert rule.scan_file(module, referenced, _config(tmp_path, load_query="src")) == []
    assert query.resolve() in referenced


def test_loader_path_held_in_a_constant_is_resolved(tmp_path: Path) -> None:
    """A loader path is routinely a named constant, iterated by a loop or a comprehension;
    reading only the literal argument made every such file look orphaned."""
    ddl = tmp_path / "src" / "graph" / "ddl"
    ddl.mkdir(parents=True)
    (ddl / "0001.cypher").write_text(
        "CREATE CONSTRAINT IF NOT EXISTS FOR (n:X) REQUIRE n.id IS UNIQUE;\n"
    )
    (ddl / "0002.cypher").write_text("CREATE VECTOR INDEX v IF NOT EXISTS FOR (n:Y) ON (n.e);\n")
    header = '_DDL = (\n    "graph/ddl/0001.cypher",\n    "graph/ddl/0002.cypher",\n)\n'
    config = rule.Config(
        root=tmp_path,
        loaders={"load_statements": tmp_path / "src"},
        query_dirs=(tmp_path / "src",),
    )
    for body in (
        "for f in _DDL:\n    load_statements(f)\n",
        "xs = [s for f in _DDL for s in load_statements(f)]\n",
    ):
        module = tmp_path / "m.py"
        module.write_text(header + body)
        referenced: set[Path] = set()
        assert rule.scan_file(module, referenced, config) == [], body
        assert rule.orphan_findings(referenced, config) == [], body


def test_loader_path_built_by_an_fstring_family_is_resolved(tmp_path: Path) -> None:
    write = tmp_path / "src" / "queries" / "write"
    write.mkdir(parents=True)
    for rel_type in ("acted_in", "directed"):
        (write / f"merge_{rel_type}.cypher").write_text("MERGE (a)-[r:X]->(b)\n")
    module = tmp_path / "m.py"
    module.write_text(
        "Q = {\n"
        '    t: load_query(f"queries/write/merge_{t.lower()}.cypher")\n'
        '    for t in ("ACTED_IN", "DIRECTED")\n'
        "}\n"
    )
    config = rule.Config(
        root=tmp_path, loaders={"load_query": tmp_path / "src"}, query_dirs=(tmp_path / "src",)
    )
    referenced: set[Path] = set()
    assert rule.scan_file(module, referenced, config) == []
    assert rule.orphan_findings(referenced, config) == []
    assert len(referenced) == 2


def test_loader_path_constant_pointing_nowhere_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    module = tmp_path / "m.py"
    module.write_text('PATH = "does_not_exist.cypher"\nload_query(PATH)\n')
    findings = rule.scan_file(module, set(), _config(tmp_path, load_query="src"))
    assert [f.rule for f in findings] == ["no-inline-queries-missing-file"]


def test_orphan_query_file_is_flagged_and_an_exempt_one_is_not(tmp_path: Path) -> None:
    (tmp_path / "src" / "ddl").mkdir(parents=True)
    (tmp_path / "src" / "orphan.cypher").write_text("MATCH (n) RETURN n\n")
    (tmp_path / "src" / "ddl" / "0001.cypher").write_text("CREATE INDEX a FOR (n:A) ON (n.x);\n")
    config = rule.Config(
        root=tmp_path,
        loaders={"load_query": tmp_path / "src"},
        query_dirs=(tmp_path / "src",),
        orphan_exempt_globs=("**/ddl/*.cypher",),
    )
    findings = rule.orphan_findings(set(), config)
    assert [(f.rule, f.path) for f in findings] == [
        ("no-inline-queries-orphan-file", "src/orphan.cypher")
    ]


def test_referenced_query_file_is_not_an_orphan(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    used = tmp_path / "src" / "used.cypher"
    used.write_text("MATCH (n) RETURN n\n")
    config = rule.Config(
        root=tmp_path, loaders={"load_query": tmp_path / "src"}, query_dirs=(tmp_path / "src",)
    )
    assert rule.orphan_findings({used.resolve()}, config) == []


#: Query shapes a naive detector misses: label-less patterns, procedures, DDL wrapped across
#: lines, and the SQL forms that share `CREATE ... INDEX` with Cypher.
GAP_SHAPES = {
    "label_less_match": ("MATCH (n {id: $target_id}) RETURN count(n) AS cnt", "cypher"),
    "call_db": ("CALL db.index.vector.queryNodes($name, $k, $vec) YIELD node, score", "cypher"),
    "unwind": ("UNWIND $rows AS row MERGE (n:Person {id: row.id})", "cypher"),
    "show_indexes": ("SHOW INDEXES YIELD name, state", "cypher"),
    "detach_delete": ("MATCH (n) DETACH DELETE n", "cypher"),
    "vector_index": (
        "CREATE VECTOR INDEX vi_x IF NOT EXISTS\nFOR (n:Movie) ON (n.plot_embedding)",
        "cypher",
    ),
    "create_constraint": (
        "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Person) REQUIRE n.id IS UNIQUE",
        "cypher",
    ),
    "multiline_constraint": (
        "CREATE CONSTRAINT IF NOT EXISTS\nFOR (n:Entity)\nREQUIRE n.id IS UNIQUE\n",
        "cypher",
    ),
    "multiline_fulltext": (
        "CREATE FULLTEXT INDEX names IF NOT EXISTS\nFOR (n:Person|Movie)\nON EACH [n.name]\n",
        "cypher",
    ),
    "multiline_rel_index": (
        "CREATE INDEX IF NOT EXISTS\nFOR ()-[r:ACTED_IN]-()\nON (r.roles)\n",
        "cypher",
    ),
    "create_view": ("CREATE VIEW active_order AS SELECT id FROM orders", "sql"),
    "create_trigger": ("CREATE TRIGGER t AFTER INSERT ON orders BEGIN SELECT 1; END", "sql"),
    "create_unique_index": ("CREATE UNIQUE INDEX ix_order_code ON orders (code)", "sql"),
    "truncate": ("TRUNCATE TABLE audit_event", "sql"),
    "insert_or_replace": ("INSERT OR REPLACE INTO settings VALUES (1, 2)", "sql"),
    "sql_plain_index": ("CREATE INDEX ix_order_code ON orders (code)", "sql"),
    "sql_create_table": ("CREATE TABLE orders (id TEXT PRIMARY KEY, code TEXT)", "sql"),
}


@pytest.mark.parametrize(("text", "kind"), list(GAP_SHAPES.values()), ids=list(GAP_SHAPES))
def test_query_shapes_are_detected(text: str, kind: str) -> None:
    assert rule.looks_like_query_text(text) == kind


#: Shapes that must never be a finding: prose merely containing a clause word, short tokens.
CLEAN_SHAPES = {
    "prose_select": "Select an item from the list to match your search",
    "log_message": "created index page for the update set by the operator",
    "word_match_alone": "no match found",
    "short_token": "SELECT",
    "identifier_like": "update_set",
}


@pytest.mark.parametrize("text", list(CLEAN_SHAPES.values()), ids=list(CLEAN_SHAPES))
def test_prose_is_not_mistaken_for_query_text(text: str) -> None:
    assert rule.looks_like_query_text(text) is None


def test_orm_expressions_are_not_flagged(tmp_path: Path) -> None:
    """ORM and Core build Python objects, not query strings -- the reason this rule reads
    string constants off the AST rather than grepping text."""
    path = tmp_path / "repo.py"
    path.write_text(
        "stmt = select(User).where(User.id == uid).order_by(User.code)\n"
        "op.create_table('orders', sa.Column('id', sa.String()))\n"
        "op.create_index('ix_order_code', 'orders', ['code'], unique=True)\n"
    )
    assert rule.scan_file(path, set(), _config(tmp_path)) == []


def test_docstring_quoting_cypher_is_not_flagged(tmp_path: Path) -> None:
    path = tmp_path / "doc.py"
    path.write_text('"""Bans MATCH (n:Person {id: $id}) RETURN n outside a .cypher file."""\n')
    assert rule.scan_file(path, set(), _config(tmp_path)) == []


def test_excluded_paths_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "tests" / "helpers.py"
    path.parent.mkdir()
    path.write_text('Q = "MATCH (n:Person {id: $id}) RETURN n"\n')
    assert rule.scan_file(path, set(), _config(tmp_path)) == []


def test_migration_raw_execute_is_flagged_but_core_ddl_is_not(tmp_path: Path) -> None:
    """Only raw text reaching `op.execute`/`sa.text` counts in a migration; Core DDL is the
    sanctioned way to write one."""
    migration = tmp_path / "alembic" / "versions" / "0002_seed.py"
    migration.parent.mkdir(parents=True)
    migration.write_text(
        "def upgrade() -> None:\n"
        "    op.create_table('vocabulary', sa.Column('id', sa.String()))\n"
        "    op.bulk_insert(vocabulary, [{'id': 'a'}])\n"
        "def downgrade() -> None:\n"
        '    op.execute("DELETE FROM vocabulary")\n'
    )
    findings = rule.scan_file(migration, set(), _config(tmp_path))
    assert [(f.rule, f.line) for f in findings] == [("no-inline-queries-sql", 5)]


def test_the_cli_runs_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "used.cypher").write_text("MATCH (n) RETURN n\n")
    (tmp_path / "src" / "orphan.cypher").write_text("MATCH (n) RETURN n\n")
    (tmp_path / "app.py").write_text('Q = load_query("used.cypher")\n')
    argv = [str(tmp_path), "--loader", "load_query=src", "--query-dir", "src"]
    assert rule.main(argv) == 1
    assert "no-inline-queries-orphan-file" in capsys.readouterr().out
    (tmp_path / "src" / "orphan.cypher").unlink()
    assert rule.main(argv) == 0
