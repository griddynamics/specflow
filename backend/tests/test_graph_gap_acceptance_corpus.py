"""Acceptance tests of graph.gap: Corpus integration (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("09")
def test_fr_gcor_01_corpus_reader_apply_active_amendment_lineage_newest() -> None:
    """Given an amendment on `orders-spec.md`, then the build reads the amended text and the stored document is
    byte-identical.
    """


@pending("09")
def test_fr_gcor_02_effective_revision_id_hash_source_revision_id() -> None:
    """Given an unchanged document and unchanged amendments, then two corpus reads give the same effective revision
    id.
    """


@pending("09")
def test_fr_gcor_03_clarifications_document_generated_applied_pending_clarifications_placement() -> None:
    """Given one reverted and one applied clarification, then the document holds only the applied one's section."""


@pending("09")
def test_fr_gcor_04_after_apply_build_amended_fragments_re_extracted() -> None:
    """Given one amended section, then the build's change report touches only that fragment's nodes and the plan
    re-plans only their stories.
    """


@pending("10")
def test_fr_gcor_05_download_answers_export_effective_text_document() -> None:
    """Given an amended document, then the export equals the text the build read."""
