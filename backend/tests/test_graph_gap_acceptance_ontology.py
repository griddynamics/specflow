"""Acceptance tests of graph.gap: Ontology, rules and ownership (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("01", "covered by test_graph_gap_ontology")
def test_fr_gont_01_load_ontology_merge_shipped_gap_ontology_file() -> None:
    """When called without arguments, then `Gap` and `Clarification` are owned by `gap`, the relations `ABOUT`,
    `IMPACTS`, `CITES`, `FOLLOWS_UP`, `ANSWERS`, `PLACED_IN`, `GROUNDED_IN`, `REPLACES` are owned by `gap`, and no
    build or plan type, relation or vocabulary differs from the files without gap.
    """


@pending("01", "covered by test_graph_gap_ontology")
def test_fr_gont_02_installing_changing_gap_ontology_file_change_extraction() -> None:
    """Given a project built without gap, when gap is installed and an update build runs on unchanged documents, then
    the build reports no change.
    """


@pending("01", "covered by test_graph_gap_rules")
def test_fr_gont_03_load_gap_rules_validate_rules_against_merged() -> None:
    """Given rules naming an unknown type, an unknown relation and an unknown gap kind, then the error names all
    three.
    """


@pending("01", "covered by test_graph_gap_rules; reviewed again in 04")
def test_fr_gont_04_graph_gap_reach_spec_types_relations_attributes() -> None:
    """Inspection: the structure test finds no build label, relation or attribute name as a literal under
    `graph.gap`.
    """


@pending("01", "covered by test_graph_gap_schema and test_graph_gap_schema_live")
def test_fr_gont_05_ensure_gap_schema_create_graph_gap_constraint() -> None:
    """When it runs twice, then the second run returns `()`."""


@pending("11", "partial: 05")
def test_fr_gown_01_graph_gap_write_types_ontology_file_never() -> None:
    """Given a full gap run, answers, an apply build and a revert on the shop corpus, then every build-owned node,
    relation and property written by `graph.gap` is none (store audit).
    """


@pending("11", "partial: 03, 05")
def test_fr_gown_02_gap_query_graph_relational_bind_filter_project() -> None:
    """Given two projects with the same corpus, when project A runs, is answered and reverted, then nothing of
    project B is read or changed.
    """


@pending("01", "covered by the import contracts in importlinter.toml")
def test_fr_gown_03_graph_gap_import_graph_core_graph_library() -> None:
    """Inspection: the import contract test passes."""


@pending("11", "partial: 01, 03")
def test_fr_gown_04_options_amendments_runs_seeds_sites_verdicts_section() -> None:
    """Given a full run with answers, then the graph holds only `Gap` and `Clarification` gap nodes."""
