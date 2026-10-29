"""Port contract suites and the file implementations (plan 2.T1; api.md "Ports"; tech-specs 5.1).

The ``*Contract`` classes are reusable: a host store subclasses one and provides the fixtures named
in its docstring. Here they run against the file implementations and against minimal in-memory
stores (which also prove a store with only the required methods satisfies the suites).
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from graph.build import files as files_module
from graph.build.files import (
    FileBuildStore,
    FileCorpusSource,
    FileExtractionStore,
    FileStores,
    open_file_stores,
)
from graph.build.models import STEPS, BuildMode, BuildResult, BuildStatus, StepName
from graph.build.options import BuildOptions
from graph.build.ports import (
    EXTRACTION_KINDS,
    BuildRecord,
    BuildStore,
    CorpusRevision,
    CorpusSource,
    ExtractionKind,
    ExtractionStore,
    JsonObject,
    StepState,
)
from graph.core.errors import ConfigurationError, CorpusError, GraphError, InfrastructureUnavailableError
from graph.core.files import FileSpecIdRegistry
from graph.core.identity import SpecIdRegistry, SpecIdRow

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
FINGERPRINT = "fp-1"
requires_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")


def temporary_files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.name.endswith(".tmp"))


def all_files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


# CorpusSource ---------------------------------------------------------------------------------------


class CorpusHarness:
    """What the corpus contract needs from an implementation: a source and a way to change its corpus."""

    source: CorpusSource

    def put(self, lineage_id: str, markdown: str) -> None:
        raise NotImplementedError

    def remove(self, lineage_id: str) -> None:
        raise NotImplementedError


class FileCorpusHarness(CorpusHarness):
    def __init__(self, folder: Path, index_root: Path) -> None:
        self.folder = folder
        self.index_root = index_root
        folder.mkdir(parents=True, exist_ok=True)
        self.source = FileCorpusSource(folder, index_root)

    def put(self, lineage_id: str, markdown: str) -> None:
        path = self.folder / lineage_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")

    def remove(self, lineage_id: str) -> None:
        (self.folder / lineage_id).unlink()


async def snapshot(source: CorpusSource, project_id: str = "proj", *, dry_run: bool = False) -> list[CorpusRevision]:
    return await source.snapshot(project_id, dry_run=dry_run)


def by_lineage(revisions: Sequence[CorpusRevision]) -> dict[str, CorpusRevision]:
    return {revision.lineage_id: revision for revision in revisions}


class CorpusSourceContract:
    """Contract of ``CorpusSource``. Subclasses provide the fixture ``corpus`` (a ``CorpusHarness``)."""

    async def test_an_empty_corpus_has_no_lineages(self, corpus: CorpusHarness) -> None:
        assert await snapshot(corpus.source) == []

    async def test_each_lineage_has_a_complete_revision(self, corpus: CorpusHarness) -> None:
        corpus.put("a.md", "# Alpha\n\nText.\n")
        (revision,) = await snapshot(corpus.source)

        assert isinstance(revision, CorpusRevision)
        assert revision.lineage_id == "a.md"
        assert revision.markdown == "# Alpha\n\nText.\n"
        assert revision.revision_id
        assert revision.version >= 1
        assert revision.path
        assert revision.title == "Alpha"

    async def test_lineages_come_in_a_stable_document_order(self, corpus: CorpusHarness) -> None:
        for name in ("c.md", "a.md", "b.md"):
            corpus.put(name, f"# {name}\n")
        first = [revision.lineage_id for revision in await snapshot(corpus.source)]
        second = [revision.lineage_id for revision in await snapshot(corpus.source)]

        assert first == second == ["a.md", "b.md", "c.md"]

    async def test_unchanged_content_keeps_revision_and_version(self, corpus: CorpusHarness) -> None:
        corpus.put("a.md", "# A\n")
        first = by_lineage(await snapshot(corpus.source))["a.md"]
        second = by_lineage(await snapshot(corpus.source))["a.md"]
        third = by_lineage(await snapshot(corpus.source))["a.md"]

        assert (first.revision_id, first.version) == (second.revision_id, second.version)
        assert (first.revision_id, first.version) == (third.revision_id, third.version)

    async def test_version_increments_only_when_the_content_changes(self, corpus: CorpusHarness) -> None:
        corpus.put("a.md", "# A\n")
        corpus.put("b.md", "# B\n")
        v1 = by_lineage(await snapshot(corpus.source))
        corpus.put("a.md", "# A\n\nMore.\n")
        v2 = by_lineage(await snapshot(corpus.source))
        v3 = by_lineage(await snapshot(corpus.source))

        assert v2["a.md"].version == v1["a.md"].version + 1
        assert v2["a.md"].revision_id != v1["a.md"].revision_id
        assert v2["b.md"].version == v1["b.md"].version
        assert v2["b.md"].revision_id == v1["b.md"].revision_id
        assert v3["a.md"].version == v2["a.md"].version

    async def test_a_new_lineage_does_not_move_the_others(self, corpus: CorpusHarness) -> None:
        corpus.put("a.md", "# A\n")
        before = by_lineage(await snapshot(corpus.source))["a.md"]
        corpus.put("b.md", "# B\n")
        after = by_lineage(await snapshot(corpus.source))

        assert after["a.md"].version == before.version
        assert after["b.md"].version == 1

    async def test_the_same_content_in_two_lineages_shares_a_revision_id_but_not_a_lineage(
        self, corpus: CorpusHarness
    ) -> None:
        corpus.put("a.md", "# Same\n")
        corpus.put("b.md", "# Same\n")
        found = by_lineage(await snapshot(corpus.source))

        assert found["a.md"].revision_id == found["b.md"].revision_id
        assert found["a.md"].lineage_id != found["b.md"].lineage_id

    async def test_a_removed_lineage_leaves_the_snapshot(self, corpus: CorpusHarness) -> None:
        corpus.put("a.md", "# A\n")
        corpus.put("b.md", "# B\n")
        await snapshot(corpus.source)
        corpus.remove("a.md")

        assert [revision.lineage_id for revision in await snapshot(corpus.source)] == ["b.md"]

    async def test_a_dry_run_gives_the_same_revisions_and_never_advances_versions(self, corpus: CorpusHarness) -> None:
        corpus.put("a.md", "# A\n")
        dry_new = by_lineage(await snapshot(corpus.source, dry_run=True))
        dry_again = by_lineage(await snapshot(corpus.source, dry_run=True))
        real = by_lineage(await snapshot(corpus.source))

        assert dry_new["a.md"].version == dry_again["a.md"].version == real["a.md"].version == 1

        corpus.put("a.md", "# A\n\nEdited.\n")
        dry_edit = by_lineage(await snapshot(corpus.source, dry_run=True))
        dry_edit_again = by_lineage(await snapshot(corpus.source, dry_run=True))
        real_edit = by_lineage(await snapshot(corpus.source))

        assert dry_edit["a.md"].version == dry_edit_again["a.md"].version == 2
        assert real_edit["a.md"].version == 2  # the dry runs did not count as versions
        assert dry_edit["a.md"].revision_id == real_edit["a.md"].revision_id

    async def test_projects_track_their_versions_independently(self, corpus: CorpusHarness) -> None:
        corpus.put("a.md", "# A\n")
        await snapshot(corpus.source, "one")
        corpus.put("a.md", "# A\n\nEdited.\n")
        await snapshot(corpus.source, "one")

        assert by_lineage(await snapshot(corpus.source, "one"))["a.md"].version == 2
        assert by_lineage(await snapshot(corpus.source, "two"))["a.md"].version == 1


class TestFileCorpusSource(CorpusSourceContract):
    @pytest.fixture
    def corpus(self, tmp_path: Path) -> CorpusHarness:
        return FileCorpusHarness(tmp_path / "corpus", tmp_path / "root")

    @pytest.fixture
    def harness(self, corpus: CorpusHarness) -> FileCorpusHarness:
        assert isinstance(corpus, FileCorpusHarness)
        return corpus

    async def test_lineages_come_from_nested_folders_with_posix_relative_paths(
        self, harness: FileCorpusHarness
    ) -> None:
        harness.put("top.md", "# Top\n")
        harness.put("a/b/deep.md", "# Deep\n")
        harness.put("a/mid.md", "# Mid\n")
        revisions = await snapshot(harness.source)

        assert [revision.lineage_id for revision in revisions] == sorted(["top.md", "a/b/deep.md", "a/mid.md"])
        assert {revision.lineage_id: revision.path for revision in revisions} == {
            "top.md": "top.md",
            "a/b/deep.md": "a/b/deep.md",
            "a/mid.md": "a/mid.md",
        }

    async def test_revision_id_is_the_sha256_of_the_bytes(self, harness: FileCorpusHarness) -> None:

        harness.put("a.md", "# Ünï\n")
        (revision,) = await snapshot(harness.source)
        assert revision.revision_id == hashlib.sha256("# Ünï\n".encode()).hexdigest()
        assert revision.markdown == "# Ünï\n"

    async def test_only_markdown_files_count(self, harness: FileCorpusHarness) -> None:
        harness.put("a.md", "# A\n")
        harness.put("notes.txt", "text")
        harness.put("data.json", "{}")
        harness.put("README", "readme")
        (harness.folder / "folder.md").mkdir()

        assert [revision.lineage_id for revision in await snapshot(harness.source)] == ["a.md"]

    async def test_dot_files_and_dot_folders_are_skipped(self, harness: FileCorpusHarness) -> None:
        harness.put("visible.md", "# V\n")
        harness.put(".hidden.md", "# H\n")
        harness.put(".git/inside.md", "# G\n")
        harness.put("sub/.draft.md", "# D\n")
        harness.put("sub/.cache/deep.md", "# C\n")
        harness.put("sub/ok.md", "# O\n")

        assert [revision.lineage_id for revision in await snapshot(harness.source)] == ["sub/ok.md", "visible.md"]

    async def test_title_is_the_first_heading(self, harness: FileCorpusHarness) -> None:
        harness.put("a.md", "intro text\n\n## Second level first ##\n\n# Later\n")
        (revision,) = await snapshot(harness.source)
        assert revision.title == "Second level first"

    async def test_title_ignores_headings_in_code_and_comments(self, harness: FileCorpusHarness) -> None:
        harness.put("a.md", "<!-- # hidden -->\n```\n# code\n```\n# Real\n")
        (revision,) = await snapshot(harness.source)
        assert revision.title == "Real"

    @pytest.mark.parametrize("body", ["no heading at all\n", "", "<!-- only a comment -->\n"])
    async def test_title_falls_back_to_the_file_name_without_extension(
        self, harness: FileCorpusHarness, body: str
    ) -> None:
        harness.put("area/checkout-spec.md", body)
        (revision,) = await snapshot(harness.source)
        assert revision.title == "checkout-spec"

    async def test_a_missing_folder_is_a_corpus_error(self, tmp_path: Path) -> None:
        source = FileCorpusSource(tmp_path / "absent", tmp_path / "root")
        with pytest.raises(CorpusError) as raised:
            await source.snapshot("proj")
        assert raised.value.code == "corpus_folder_missing"

    async def test_a_file_that_is_not_utf8_is_a_corpus_error_naming_the_lineage(
        self, harness: FileCorpusHarness
    ) -> None:
        harness.put("good.md", "# Good\n")
        (harness.folder / "sub").mkdir()
        (harness.folder / "sub" / "bad.md").write_bytes(b"# Bad \xff\xfe\n")

        with pytest.raises(CorpusError) as raised:
            await snapshot(harness.source)
        assert raised.value.code == "corpus_unreadable"
        assert raised.value.fields["lineage_id"] == "sub/bad.md"
        assert "Bad" not in str(raised.value)

    @requires_non_root
    async def test_an_unreadable_file_is_a_corpus_error(self, harness: FileCorpusHarness) -> None:
        harness.put("locked.md", "# Locked\n")
        path = harness.folder / "locked.md"
        path.chmod(0)
        try:
            with pytest.raises(CorpusError) as raised:
                await snapshot(harness.source)
        finally:
            path.chmod(0o600)
        assert raised.value.code == "corpus_unreadable"
        assert raised.value.fields["lineage_id"] == "locked.md"

    async def test_a_corpus_error_writes_nothing(self, harness: FileCorpusHarness) -> None:
        harness.put("good.md", "# Good\n")
        (harness.folder / "bad.md").write_bytes(b"\xff")
        with pytest.raises(CorpusError):
            await snapshot(harness.source)
        assert not harness.index_root.exists()

    async def test_the_version_index_lives_under_the_root_and_project(self, harness: FileCorpusHarness) -> None:
        harness.put("a.md", "# A\n")
        await snapshot(harness.source, "proj-7")

        index = harness.index_root / "proj-7" / "corpus-index.json"
        assert all_files(harness.index_root) == ["proj-7/corpus-index.json"]
        stored = json.loads(index.read_text(encoding="utf-8"))
        assert stored["a.md"]["version"] == 1
        assert len(stored["a.md"]["revision_id"]) == 64

    async def test_dry_run_writes_nothing(self, harness: FileCorpusHarness) -> None:
        harness.put("a.md", "# A\n")
        await snapshot(harness.source, dry_run=True)
        assert not harness.index_root.exists()

        await snapshot(harness.source)
        before = (harness.index_root / "proj" / "corpus-index.json").read_bytes()
        harness.put("a.md", "# A\n\nEdited.\n")
        harness.put("b.md", "# B\n")
        await snapshot(harness.source, dry_run=True)
        assert (harness.index_root / "proj" / "corpus-index.json").read_bytes() == before
        assert temporary_files(harness.index_root) == []

    async def test_an_unchanged_corpus_does_not_rewrite_the_index(self, harness: FileCorpusHarness) -> None:
        harness.put("a.md", "# A\n")
        await snapshot(harness.source)
        index = harness.index_root / "proj" / "corpus-index.json"
        os.utime(index, (1_000_000, 1_000_000))
        await snapshot(harness.source)
        assert index.stat().st_mtime == 1_000_000

    async def test_the_version_survives_a_new_source_object(self, harness: FileCorpusHarness) -> None:
        harness.put("a.md", "# A\n")
        await snapshot(harness.source)
        harness.put("a.md", "# A\n\nEdited.\n")
        reopened = FileCorpusSource(harness.folder, harness.index_root)

        assert by_lineage(await snapshot(reopened))["a.md"].version == 2

    async def test_reverting_content_still_counts_as_a_change(self, harness: FileCorpusHarness) -> None:
        harness.put("a.md", "# A\n")
        await snapshot(harness.source)
        harness.put("a.md", "# B\n")
        await snapshot(harness.source)
        harness.put("a.md", "# A\n")
        assert by_lineage(await snapshot(harness.source))["a.md"].version == 3

    @pytest.mark.parametrize("content", ["[1, 2]", "{broken", ""])
    async def test_a_corrupt_version_index_is_infrastructure_unavailable(
        self, harness: FileCorpusHarness, content: str
    ) -> None:
        harness.put("a.md", "# A\n")
        index = harness.index_root / "proj" / "corpus-index.json"
        index.parent.mkdir(parents=True)
        index.write_text(content, encoding="utf-8")

        with pytest.raises(InfrastructureUnavailableError) as raised:
            await snapshot(harness.source)
        assert raised.value.code == "corpus_index_unreadable"

    async def test_an_invalid_project_id_is_a_configuration_error(self, harness: FileCorpusHarness) -> None:
        with pytest.raises(ConfigurationError):
            await snapshot(harness.source, "../escape")

    async def test_concurrent_snapshots_agree(self, harness: FileCorpusHarness) -> None:
        harness.put("a.md", "# A\n")
        results = await asyncio.gather(*(snapshot(harness.source) for _ in range(6)))
        assert all(result == results[0] for result in results)
        assert results[0][0].version == 1


def test_a_corpus_revision_with_missing_fields_is_a_corpus_error() -> None:
    with pytest.raises(CorpusError) as raised:
        CorpusRevision(lineage_id="", revision_id="r", version=1, path="p", title="t", markdown="m")
    assert raised.value.code == "corpus_invalid_revision"
    assert "lineage_id" in str(raised.value.fields["fields"])

    with pytest.raises(CorpusError):
        CorpusRevision(lineage_id="a", revision_id="r", version=-1, path="p", title="t", markdown="m")
    with pytest.raises(CorpusError):
        CorpusRevision(lineage_id="a", revision_id="r", path="p", title="t")


# ExtractionStore ------------------------------------------------------------------------------------


def payload(**extra: Any) -> JsonObject:
    return {"items": [{"name": "Ünï", "n": 1, "ok": True, "none": None}], "ratio": 0.5, **extra}


class ExtractionStoreContract:
    """Contract of ``ExtractionStore``. Subclasses provide the fixture ``store``."""

    async def test_an_absent_key_is_a_miss(self, store: ExtractionStore) -> None:
        assert await store.get("nodes", "p|frag-1", FINGERPRINT) is None

    async def test_put_then_get_round_trips_the_payload(self, store: ExtractionStore) -> None:
        await store.put("nodes", "p|frag-1", FINGERPRINT, payload())
        assert await store.get("nodes", "p|frag-1", FINGERPRINT) == payload()

    @pytest.mark.parametrize("kind", EXTRACTION_KINDS)
    async def test_every_kind_round_trips(self, store: ExtractionStore, kind: ExtractionKind) -> None:
        await store.put(kind, "p|key", FINGERPRINT, payload(kind=kind))
        assert await store.get(kind, "p|key", FINGERPRINT) == payload(kind=kind)

    def test_the_kinds_include_vectors_and_decisions(self) -> None:
        assert {"nodes", "edges", "attributes", "vectors", "decisions"} <= set(EXTRACTION_KINDS)

    async def test_the_same_key_in_two_kinds_is_two_entries(self, store: ExtractionStore) -> None:
        await store.put("nodes", "p|k", FINGERPRINT, {"v": "nodes"})
        await store.put("edges", "p|k", FINGERPRINT, {"v": "edges"})

        assert await store.get("nodes", "p|k", FINGERPRINT) == {"v": "nodes"}
        assert await store.get("edges", "p|k", FINGERPRINT) == {"v": "edges"}
        assert await store.get("vectors", "p|k", FINGERPRINT) is None

    async def test_keys_with_separators_and_unicode_are_distinct(self, store: ExtractionStore) -> None:
        keys = ["p|m|abc", "p|m|abd", "p|a|b|c", "p/m/abc", "pöm|ü", "p|m|abc ", "P|m|abc"]
        for index, key in enumerate(keys):
            await store.put("vectors", key, FINGERPRINT, {"index": index})
        for index, key in enumerate(keys):
            assert await store.get("vectors", key, FINGERPRINT) == {"index": index}

    async def test_a_decisions_key_in_the_documented_shape_round_trips(self, store: ExtractionStore) -> None:
        key = "proj|Module|surface|orders service|Module|surface|order management"
        await store.put("decisions", key, FINGERPRINT, {"merged": False})
        assert await store.get("decisions", key, FINGERPRINT) == {"merged": False}

    async def test_another_fingerprint_is_a_miss(self, store: ExtractionStore) -> None:
        await store.put("nodes", "p|k", "fp-old", payload())

        assert await store.get("nodes", "p|k", "fp-new") is None
        assert await store.get("nodes", "p|k", "") is None
        assert await store.get("nodes", "p|k", "fp-old") == payload()

    async def test_put_replaces_the_entry_and_its_fingerprint(self, store: ExtractionStore) -> None:
        await store.put("nodes", "p|k", "fp-old", {"v": 1})
        await store.put("nodes", "p|k", "fp-new", {"v": 2})

        assert await store.get("nodes", "p|k", "fp-new") == {"v": 2}
        assert await store.get("nodes", "p|k", "fp-old") is None

    async def test_an_empty_payload_is_a_hit_not_a_miss(self, store: ExtractionStore) -> None:
        await store.put("nodes", "p|k", FINGERPRINT, {})
        assert await store.get("nodes", "p|k", FINGERPRINT) == {}

    async def test_the_caller_cannot_change_a_stored_payload(self, store: ExtractionStore) -> None:
        original = {"list": [1, 2]}
        await store.put("nodes", "p|k", FINGERPRINT, original)
        original["list"].append(3)
        first = await store.get("nodes", "p|k", FINGERPRINT)
        assert first == {"list": [1, 2]}
        assert first is not None
        first["list"].append(9)
        assert await store.get("nodes", "p|k", FINGERPRINT) == {"list": [1, 2]}

    async def test_concurrent_puts_and_gets_do_not_lose_entries(self, store: ExtractionStore) -> None:
        await asyncio.gather(*(store.put("nodes", f"k{index}", FINGERPRINT, {"v": index}) for index in range(30)))
        results = await asyncio.gather(*(store.get("nodes", f"k{index}", FINGERPRINT) for index in range(30)))
        assert results == [{"v": index} for index in range(30)]


class BatchExtractionStoreContract:
    """Contract of the optional ``get_many``/``put_many``. Subclasses provide the fixture ``store``."""

    @pytest.fixture
    def batch(self, store: ExtractionStore) -> Any:
        return store

    async def test_get_many_returns_only_hits_with_the_fingerprint(self, batch: Any) -> None:
        await batch.put("nodes", "k1", FINGERPRINT, {"v": 1})
        await batch.put("nodes", "k2", FINGERPRINT, {"v": 2})
        await batch.put("nodes", "k3", "other", {"v": 3})
        await batch.put("edges", "k4", FINGERPRINT, {"v": 4})

        found = await batch.get_many("nodes", ["k1", "k2", "k3", "k4", "missing"], FINGERPRINT)
        assert found == {"k1": {"v": 1}, "k2": {"v": 2}}

    async def test_get_many_handles_no_keys_and_repeated_keys(self, batch: Any) -> None:
        await batch.put("nodes", "k1", FINGERPRINT, {"v": 1})
        assert await batch.get_many("nodes", [], FINGERPRINT) == {}
        assert await batch.get_many("nodes", ["k1", "k1", "k1"], FINGERPRINT) == {"k1": {"v": 1}}

    async def test_put_many_is_visible_to_get_and_get_many(self, batch: Any) -> None:
        entries = {f"k{index}": {"v": index} for index in range(25)}
        await batch.put_many("vectors", FINGERPRINT, entries)

        assert await batch.get("vectors", "k7", FINGERPRINT) == {"v": 7}
        assert await batch.get_many("vectors", list(entries), FINGERPRINT) == entries
        assert await batch.get("nodes", "k7", FINGERPRINT) is None

    async def test_put_many_replaces_entries_and_accepts_nothing(self, batch: Any) -> None:
        await batch.put("vectors", "k1", "old", {"v": 1})
        await batch.put_many("vectors", FINGERPRINT, {})
        await batch.put_many("vectors", FINGERPRINT, {"k1": {"v": 2}})

        assert await batch.get("vectors", "k1", FINGERPRINT) == {"v": 2}
        assert await batch.get("vectors", "k1", "old") is None


class MemoryExtractionStore:
    """A host-style store with only the two required methods."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[str, str]] = {}

    async def get(self, kind: ExtractionKind, key: str, fingerprint: str) -> JsonObject | None:
        entry = self._entries.get((kind, key))
        return None if entry is None or entry[0] != fingerprint else json.loads(entry[1])

    async def put(self, kind: ExtractionKind, key: str, fingerprint: str, payload: JsonObject) -> None:
        self._entries[(kind, key)] = (fingerprint, json.dumps(payload))


class TestMemoryExtractionStore(ExtractionStoreContract):
    @pytest.fixture
    def store(self) -> ExtractionStore:
        return MemoryExtractionStore()


class TestFileExtractionStore(ExtractionStoreContract, BatchExtractionStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> FileExtractionStore:
        return FileExtractionStore(tmp_path / "_extraction")

    @pytest.fixture
    def warnings(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
        seen: list[tuple[str, dict[str, Any]]] = []

        def spy(_logger: Any, level: int, event: str, **fields: Any) -> None:
            seen.append((event, {"level": level, **fields}))

        monkeypatch.setattr(files_module, "log_event", spy)
        return seen

    def test_the_file_store_offers_the_batch_methods(self, store: FileExtractionStore) -> None:
        assert callable(store.get_many)
        assert callable(store.put_many)

    async def test_entries_are_gzip_files_named_by_the_hash_of_the_key(
        self, store: FileExtractionStore, tmp_path: Path
    ) -> None:

        await store.put("vectors", "p|model|abc", FINGERPRINT, {"v": [1]})
        digest = hashlib.sha256(b"p|model|abc").hexdigest()
        path = tmp_path / "_extraction" / "vectors" / f"{digest}.json.gz"

        assert all_files(tmp_path) == [f"_extraction/vectors/{digest}.json.gz"]
        assert json.loads(gzip.decompress(path.read_bytes())) == {
            "key": "p|model|abc",
            "fingerprint": FINGERPRINT,
            "payload": {"v": [1]},
        }

    async def test_the_same_entry_is_written_as_the_same_bytes(self, tmp_path: Path) -> None:
        first = FileExtractionStore(tmp_path / "one")
        second = FileExtractionStore(tmp_path / "two")
        await first.put("nodes", "k", FINGERPRINT, payload())
        await second.put("nodes", "k", FINGERPRINT, payload())

        (one,) = (tmp_path / "one").rglob("*.gz")
        (two,) = (tmp_path / "two").rglob("*.gz")
        assert one.read_bytes() == two.read_bytes()

    def entry(self, tmp_path: Path) -> Path:
        (path,) = (tmp_path / "_extraction").rglob("*.json.gz")
        return path

    @pytest.mark.parametrize(
        "damage",
        [
            lambda _data: b"not gzip at all",
            lambda data: data[: len(data) // 2],
            lambda _data: b"",
            lambda _data: gzip.compress(b"{broken json"),
            lambda _data: gzip.compress(b"\xff\xfe"),
        ],
        ids=["garbage", "truncated", "empty", "bad-json", "bad-utf8"],
    )
    async def test_a_corrupt_entry_is_a_miss_with_a_warning(
        self,
        store: FileExtractionStore,
        tmp_path: Path,
        warnings: list[tuple[str, dict[str, Any]]],
        damage: Callable[[bytes], bytes],
    ) -> None:
        await store.put("nodes", "p|k", FINGERPRINT, payload())
        path = self.entry(tmp_path)
        path.write_bytes(damage(path.read_bytes()))

        assert await store.get("nodes", "p|k", FINGERPRINT) is None
        assert [event for event, _ in warnings] == ["graph.extraction_cache.unreadable"]
        assert warnings[0][1]["kind"] == "nodes"
        assert warnings[0][1]["level"] == 30  # WARNING

    async def test_a_corrupt_entry_can_be_overwritten(self, store: FileExtractionStore, tmp_path: Path) -> None:
        await store.put("nodes", "p|k", FINGERPRINT, payload())
        self.entry(tmp_path).write_bytes(b"garbage")
        await store.put("nodes", "p|k", FINGERPRINT, {"fresh": True})
        assert await store.get("nodes", "p|k", FINGERPRINT) == {"fresh": True}

    @pytest.mark.parametrize("stored", [[1, 2], "text", 5, {"key": "p|k"}, {"key": "p|k", "fingerprint": FINGERPRINT}])
    async def test_a_well_formed_file_of_the_wrong_shape_is_a_miss(
        self, store: FileExtractionStore, tmp_path: Path, stored: Any
    ) -> None:
        await store.put("nodes", "p|k", FINGERPRINT, payload())
        self.entry(tmp_path).write_bytes(gzip.compress(json.dumps(stored).encode()))
        assert await store.get("nodes", "p|k", FINGERPRINT) is None

    async def test_a_payload_that_is_not_an_object_is_a_miss(self, store: FileExtractionStore, tmp_path: Path) -> None:
        await store.put("nodes", "p|k", FINGERPRINT, payload())
        stored = {"key": "p|k", "fingerprint": FINGERPRINT, "payload": [1]}
        self.entry(tmp_path).write_bytes(gzip.compress(json.dumps(stored).encode()))
        assert await store.get("nodes", "p|k", FINGERPRINT) is None

    async def test_an_entry_stored_under_another_key_is_a_miss(
        self, store: FileExtractionStore, tmp_path: Path
    ) -> None:
        await store.put("nodes", "p|a", FINGERPRINT, {"v": "a"})
        await store.put("nodes", "p|b", FINGERPRINT, {"v": "b"})
        paths = sorted((tmp_path / "_extraction").rglob("*.json.gz"))
        a_path = next(path for path in paths if json.loads(gzip.decompress(path.read_bytes()))["key"] == "p|a")
        b_path = next(path for path in paths if path != a_path)
        b_path.write_bytes(a_path.read_bytes())  # b's file now claims to be a

        assert await store.get("nodes", "p|b", FINGERPRINT) is None
        assert await store.get("nodes", "p|a", FINGERPRINT) == {"v": "a"}

    async def test_get_many_skips_corrupt_entries_and_warns_once_with_the_count(
        self, store: FileExtractionStore, tmp_path: Path, warnings: list[tuple[str, dict[str, Any]]]
    ) -> None:
        await store.put_many("vectors", FINGERPRINT, {f"k{index}": {"v": index} for index in range(5)})
        paths = sorted((tmp_path / "_extraction").rglob("*.json.gz"))
        for path in paths[:2]:
            path.write_bytes(b"garbage")

        found = await store.get_many("vectors", [f"k{index}" for index in range(5)], FINGERPRINT)

        assert len(found) == 3
        assert [event for event, _ in warnings] == ["graph.extraction_cache.unreadable"]
        assert warnings[0][1]["entries"] == 2
        assert warnings[0][1]["kind"] == "vectors"

    async def test_a_clean_read_logs_nothing(
        self, store: FileExtractionStore, warnings: list[tuple[str, dict[str, Any]]]
    ) -> None:
        await store.put("nodes", "k", FINGERPRINT, {"v": 1})
        await store.get("nodes", "k", FINGERPRINT)
        await store.get("nodes", "absent", FINGERPRINT)
        await store.get_many("nodes", ["k", "absent"], FINGERPRINT)
        assert warnings == []

    async def test_the_warning_carries_no_payload_or_key(
        self, store: FileExtractionStore, tmp_path: Path, warnings: list[tuple[str, dict[str, Any]]]
    ) -> None:
        await store.put("nodes", "secret-key-text", FINGERPRINT, {"text": "document line"})
        self.entry(tmp_path).write_bytes(b"garbage")
        await store.get("nodes", "secret-key-text", FINGERPRINT)

        assert "secret-key-text" not in str(warnings)
        assert "document line" not in str(warnings)

    async def test_a_write_failure_is_infrastructure_unavailable(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocked"
        blocker.write_text("a file where the folder should be")
        store = FileExtractionStore(blocker)

        with pytest.raises(InfrastructureUnavailableError) as single:
            await store.put("nodes", "k", FINGERPRINT, {"v": 1})
        with pytest.raises(InfrastructureUnavailableError) as many:
            await store.put_many("nodes", FINGERPRINT, {"k": {"v": 1}})
        assert single.value.code == many.value.code == "file_store_unwritable"

    async def test_reading_from_a_store_that_never_wrote_is_a_miss(self, tmp_path: Path) -> None:
        store = FileExtractionStore(tmp_path / "never")
        assert await store.get("nodes", "k", FINGERPRINT) is None
        assert await store.get_many("nodes", ["k"], FINGERPRINT) == {}
        assert not (tmp_path / "never").exists()


# BuildStore -----------------------------------------------------------------------------------------


def record(
    build_id: str = "b1",
    *,
    project_id: str = "proj",
    mode: BuildMode = "first",
    minutes: int = 0,
    status: str = "running",
) -> BuildRecord:
    return BuildRecord.model_validate(
        {
            "build_id": build_id,
            "project_id": project_id,
            "mode": mode,
            "options": BuildOptions(),
            "ontology_fingerprint": "ontology-fp",
            "label_set": ("Module", "Feature"),
            "embedding_model": "embed-1",
            "status": status,
            "started_at": T0 + timedelta(minutes=minutes),
        }
    )


def result_of(
    build_id: str = "b1",
    *,
    project_id: str = "proj",
    mode: BuildMode = "first",
    status: BuildStatus = "succeeded",
    minutes: int = 0,
) -> BuildResult:
    return BuildResult(
        project_id=project_id,
        build_id=build_id,
        mode=mode,
        status=status,
        skipped_fragments={"doc.md#x#1": "overflow"} if status == "partially_succeeded" else {},
        failed_step="extract_nodes" if status == "failed" else None,
        error_code="llm_error" if status == "failed" else None,
        started_at=T0 + timedelta(minutes=minutes),
        finished_at=T0 + timedelta(minutes=minutes + 5),
    )


async def start(store: BuildStore, *builds: BuildRecord) -> None:
    for build in builds:
        await store.create_build(build)


class BuildStoreContract:
    """Contract of ``BuildStore``. Subclasses provide the fixture ``builds``."""

    async def test_create_then_get_round_trips_the_record(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        stored = await builds.get_build("proj", "b1")

        assert stored == record()
        assert stored is not None
        assert stored.status == "running"
        assert stored.finished_at is None
        assert stored.progress == {}
        assert stored.options == BuildOptions()
        assert not stored.finished

    async def test_an_unknown_build_is_none(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        assert await builds.get_build("proj", "other") is None
        assert await builds.get_build("elsewhere", "b1") is None
        assert await builds.get_result("proj", "other") is None
        assert await builds.get_artifact("proj", "other", "plan") is None

    async def test_a_duplicate_build_id_is_an_error_and_keeps_the_first(self, builds: BuildStore) -> None:
        await builds.create_build(record(mode="first"))
        with pytest.raises(GraphError):
            await builds.create_build(record(mode="update"))
        stored = await builds.get_build("proj", "b1")
        assert stored is not None
        assert stored.mode == "first"

    async def test_the_same_build_id_in_two_projects_is_allowed(self, builds: BuildStore) -> None:
        await start(builds, record(project_id="one"), record(project_id="two", mode="update"))
        one = await builds.get_build("one", "b1")
        two = await builds.get_build("two", "b1")
        assert one is not None and two is not None
        assert (one.mode, two.mode) == ("first", "update")

    # latest_build

    async def test_latest_build_of_an_empty_project_is_none(self, builds: BuildStore) -> None:
        assert await builds.latest_build("proj") is None
        assert await builds.latest_build("proj", finished_only=True) is None

    async def test_latest_build_is_the_most_recently_started(self, builds: BuildStore) -> None:
        await start(builds, record("b2", minutes=20), record("b1", minutes=10), record("b3", minutes=5))
        latest = await builds.latest_build("proj")
        assert latest is not None
        assert latest.build_id == "b2"

    async def test_latest_build_breaks_a_tie_on_start_time_by_build_id(self, builds: BuildStore) -> None:
        await start(builds, record("a", minutes=1), record("c", minutes=1), record("b", minutes=1))
        latest = await builds.latest_build("proj")
        assert latest is not None
        assert latest.build_id == "c"

    async def test_finished_only_skips_running_and_failed_builds(self, builds: BuildStore) -> None:
        await start(builds, record("done", minutes=1), record("failed", minutes=2), record("running", minutes=3))
        await builds.finish_build(result_of("done", minutes=1))
        await builds.finish_build(result_of("failed", status="failed", minutes=2))

        latest = await builds.latest_build("proj")
        finished = await builds.latest_build("proj", finished_only=True)
        assert latest is not None and latest.build_id == "running"
        assert finished is not None and finished.build_id == "done"

    async def test_a_partially_succeeded_build_counts_as_finished(self, builds: BuildStore) -> None:
        await start(builds, record("p", minutes=1))
        await builds.finish_build(result_of("p", status="partially_succeeded", minutes=1))
        finished = await builds.latest_build("proj", finished_only=True)
        assert finished is not None
        assert finished.build_id == "p"
        assert finished.status == "partially_succeeded"
        assert finished.finished

    async def test_finished_only_finds_nothing_while_every_build_is_unfinished(self, builds: BuildStore) -> None:
        await start(builds, record("a"), record("b", minutes=1))
        await builds.finish_build(result_of("b", status="failed", minutes=1))
        assert await builds.latest_build("proj", finished_only=True) is None

    async def test_modes_filter_the_candidates(self, builds: BuildStore) -> None:
        await start(
            builds,
            record("first", mode="first", minutes=1),
            record("update", mode="update", minutes=2),
            record("rebuild", mode="rebuild", minutes=3),
            record("update2", mode="update", minutes=4),
        )

        async def latest(modes: set[BuildMode] | None) -> str | None:
            found = await builds.latest_build("proj", modes=modes)
            return None if found is None else found.build_id

        assert await latest(None) == "update2"
        assert await latest({"update"}) == "update2"
        assert await latest({"first", "rebuild"}) == "rebuild"
        assert await latest({"first"}) == "first"
        assert await latest({"first", "rebuild", "update"}) == "update2"
        assert await latest(set()) is None

    async def test_modes_and_finished_only_combine(self, builds: BuildStore) -> None:
        await start(
            builds,
            record("first", mode="first", minutes=1),
            record("update", mode="update", minutes=2),
            record("rebuild", mode="rebuild", minutes=3),
        )
        await builds.finish_build(result_of("first", mode="first", minutes=1))
        await builds.finish_build(result_of("update", mode="update", minutes=2))

        found = await builds.latest_build("proj", finished_only=True, modes={"first", "rebuild"})
        assert found is not None and found.build_id == "first"
        assert await builds.latest_build("proj", finished_only=True, modes={"rebuild"}) is None

    async def test_latest_build_is_per_project(self, builds: BuildStore) -> None:
        await start(builds, record("mine", project_id="one", minutes=1), record("theirs", project_id="two", minutes=9))
        found = await builds.latest_build("one")
        assert found is not None and found.build_id == "mine"
        assert await builds.latest_build("three") is None

    # progress

    async def test_progress_records_each_step_state(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        await builds.put_progress("proj", "b1", "plan", "running")
        await builds.put_progress("proj", "b1", "plan", "done")
        await builds.put_progress("proj", "b1", "extract_nodes", "failed")

        stored = await builds.get_build("proj", "b1")
        assert stored is not None
        assert stored.progress == {"plan": "done", "extract_nodes": "failed"}

    async def test_progress_changes_nothing_else_in_the_record(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        await builds.put_progress("proj", "b1", "plan", "done")
        stored = await builds.get_build("proj", "b1")
        assert stored is not None
        assert stored.model_copy(update={"progress": {}}) == record()

    async def test_progress_of_an_unknown_build_is_an_error(self, builds: BuildStore) -> None:
        with pytest.raises(GraphError):
            await builds.put_progress("proj", "ghost", "plan", "done")
        assert await builds.get_build("proj", "ghost") is None

    async def test_concurrent_progress_updates_are_all_kept(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        states: list[StepState] = ["done"] * len(STEPS)
        await asyncio.gather(
            *(builds.put_progress("proj", "b1", step, state) for step, state in zip(STEPS, states, strict=True))
        )

        stored = await builds.get_build("proj", "b1")
        assert stored is not None
        assert stored.progress == dict.fromkeys(STEPS, "done")

    # finish and result

    async def test_the_result_is_none_before_the_build_finished(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        assert await builds.get_result("proj", "b1") is None

    @pytest.mark.parametrize("status", ["succeeded", "partially_succeeded", "failed"])
    async def test_finish_stores_the_result_and_updates_the_record(
        self, builds: BuildStore, status: BuildStatus
    ) -> None:
        await builds.create_build(record())
        result = result_of(status=status)
        await builds.finish_build(result)

        assert await builds.get_result("proj", "b1") == result
        stored = await builds.get_build("proj", "b1")
        assert stored is not None
        assert stored.status == status
        assert stored.finished_at == result.finished_at
        assert stored.finished is (status != "failed")

    async def test_finish_keeps_progress_and_artifacts(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        await builds.put_progress("proj", "b1", "plan", "done")
        await builds.put_artifact("proj", "b1", "plan", {"fragments": 3})
        await builds.finish_build(result_of())

        stored = await builds.get_build("proj", "b1")
        assert stored is not None and stored.progress == {"plan": "done"}
        assert await builds.get_artifact("proj", "b1", "plan") == {"fragments": 3}

    async def test_finishing_an_unknown_build_is_an_error(self, builds: BuildStore) -> None:
        with pytest.raises(GraphError):
            await builds.finish_build(result_of("ghost"))
        assert await builds.get_result("proj", "ghost") is None

    async def test_a_build_can_be_finished_again_after_a_failure(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        await builds.finish_build(result_of(status="failed"))
        await builds.finish_build(result_of(status="succeeded", minutes=30))

        stored = await builds.get_build("proj", "b1")
        assert stored is not None and stored.finished
        assert (await builds.get_result("proj", "b1")) == result_of(status="succeeded", minutes=30)

    # artifacts

    async def test_artifacts_round_trip_per_step(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        artifacts = {step: {"step": step, "text": "Ünï", "nested": {"list": [1, 2.5, None, True]}} for step in STEPS}
        for step, content in artifacts.items():
            await builds.put_artifact("proj", "b1", step, content)

        for step, content in artifacts.items():
            assert await builds.get_artifact("proj", "b1", step) == content

    async def test_a_step_without_an_artifact_is_none(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        await builds.put_artifact("proj", "b1", "plan", {"v": 1})
        assert await builds.get_artifact("proj", "b1", "resolve") is None

    async def test_putting_an_artifact_replaces_the_earlier_one(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        await builds.put_artifact("proj", "b1", "plan", {"v": 1, "old": True})
        await builds.put_artifact("proj", "b1", "plan", {"v": 2})
        assert await builds.get_artifact("proj", "b1", "plan") == {"v": 2}

    async def test_an_empty_artifact_is_not_none(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        await builds.put_artifact("proj", "b1", "persist", {})
        assert await builds.get_artifact("proj", "b1", "persist") == {}

    async def test_artifacts_are_per_build_and_per_project(self, builds: BuildStore) -> None:
        await start(builds, record("b1"), record("b2"), record("b1", project_id="other"))
        await builds.put_artifact("proj", "b1", "plan", {"owner": "b1"})
        await builds.put_artifact("proj", "b2", "plan", {"owner": "b2"})
        await builds.put_artifact("other", "b1", "plan", {"owner": "other"})

        assert await builds.get_artifact("proj", "b1", "plan") == {"owner": "b1"}
        assert await builds.get_artifact("proj", "b2", "plan") == {"owner": "b2"}
        assert await builds.get_artifact("other", "b1", "plan") == {"owner": "other"}

    async def test_a_large_artifact_round_trips(self, builds: BuildStore) -> None:
        await builds.create_build(record())
        content = {"rows": [{"id": index, "text": f"line {index} " * 20} for index in range(2000)]}
        await builds.put_artifact("proj", "b1", "conform", content)
        assert await builds.get_artifact("proj", "b1", "conform") == content


class MemoryBuildStore:
    """A host-style store keeping everything in dictionaries."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], BuildRecord] = {}
        self.results: dict[tuple[str, str], BuildResult] = {}
        self.artifacts: dict[tuple[str, str, str], str] = {}

    async def create_build(self, record: BuildRecord) -> None:
        key = (record.project_id, record.build_id)
        if key in self.records:
            raise ConfigurationError("build_exists")
        self.records[key] = record

    async def latest_build(
        self, project_id: str, *, finished_only: bool = False, modes: Any = None
    ) -> BuildRecord | None:
        candidates = [
            found
            for (project, _), found in self.records.items()
            if project == project_id
            and (not finished_only or found.finished)
            and (modes is None or found.mode in modes)
        ]
        return max(candidates, key=lambda found: (found.started_at, found.build_id), default=None)

    async def get_build(self, project_id: str, build_id: str) -> BuildRecord | None:
        return self.records.get((project_id, build_id))

    async def put_progress(self, project_id: str, build_id: str, step: StepName, state: StepState) -> None:
        current = self.records.get((project_id, build_id))
        if current is None:
            raise ConfigurationError("build_not_found")
        self.records[(project_id, build_id)] = current.model_copy(
            update={"progress": {**current.progress, step: state}}
        )

    async def finish_build(self, result: BuildResult) -> None:
        current = self.records.get((result.project_id, result.build_id))
        if current is None:
            raise ConfigurationError("build_not_found")
        self.results[(result.project_id, result.build_id)] = result
        self.records[(result.project_id, result.build_id)] = current.model_copy(
            update={"status": result.status, "finished_at": result.finished_at}
        )

    async def get_result(self, project_id: str, build_id: str) -> BuildResult | None:
        return self.results.get((project_id, build_id))

    async def put_artifact(self, project_id: str, build_id: str, step: StepName, payload: JsonObject) -> None:
        self.artifacts[(project_id, build_id, step)] = json.dumps(payload)

    async def get_artifact(self, project_id: str, build_id: str, step: StepName) -> JsonObject | None:
        stored = self.artifacts.get((project_id, build_id, step))
        return None if stored is None else json.loads(stored)


class TestMemoryBuildStore(BuildStoreContract):
    @pytest.fixture
    def builds(self) -> BuildStore:
        return MemoryBuildStore()


class TestFileBuildStore(BuildStoreContract):
    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        return tmp_path / "root"

    @pytest.fixture
    def builds(self, root: Path) -> FileBuildStore:
        return FileBuildStore(root)

    async def test_the_layout_is_one_folder_per_build_with_gzip_files(self, builds: FileBuildStore, root: Path) -> None:
        await builds.create_build(record())
        await builds.put_artifact("proj", "b1", "plan", {"v": 1})
        await builds.put_artifact("proj", "b1", "extract_nodes", {"v": 2})
        await builds.finish_build(result_of())

        assert all_files(root) == [
            "proj/builds/b1/build.json.gz",
            "proj/builds/b1/extract_nodes.json.gz",
            "proj/builds/b1/plan.json.gz",
            "proj/builds/b1/result.json.gz",
        ]
        plan = json.loads(gzip.decompress((root / "proj/builds/b1/plan.json.gz").read_bytes()))
        assert plan == {"v": 1}

    async def test_duplicate_and_unknown_builds_use_stable_codes(self, builds: FileBuildStore) -> None:
        await builds.create_build(record())
        with pytest.raises(ConfigurationError) as duplicate:
            await builds.create_build(record())
        with pytest.raises(ConfigurationError) as unknown:
            await builds.put_progress("proj", "ghost", "plan", "done")
        with pytest.raises(ConfigurationError) as finish:
            await builds.finish_build(result_of("ghost"))

        assert duplicate.value.code == "build_exists"
        assert unknown.value.code == finish.value.code == "build_not_found"
        assert unknown.value.fields["build_id"] == "ghost"

    @pytest.mark.parametrize("build_id", ["../escape", "a/b", ".hidden", "..", "/abs"])
    async def test_a_build_id_cannot_leave_the_builds_folder(self, builds: FileBuildStore, build_id: str) -> None:
        bad = record().model_copy(update={"build_id": build_id})
        with pytest.raises(ConfigurationError) as raised:
            await builds.create_build(bad)
        assert raised.value.code == "invalid_build_id"
        with pytest.raises(ConfigurationError):
            await builds.get_artifact("proj", build_id, "plan")

    async def test_an_empty_build_id_is_rejected_by_the_record_and_the_store(self, builds: FileBuildStore) -> None:
        with pytest.raises(ConfigurationError):
            await builds.get_build("proj", "")

    async def test_an_invalid_project_id_is_a_configuration_error(self, builds: FileBuildStore) -> None:
        for call in (
            builds.latest_build("../x"),
            builds.get_build("a/b", "b1"),
            builds.get_artifact("", "b1", "plan"),
        ):
            with pytest.raises(ConfigurationError):
                await call

    async def test_stray_folders_and_files_do_not_confuse_latest_build(
        self, builds: FileBuildStore, root: Path
    ) -> None:
        await builds.create_build(record("b1", minutes=1))
        builds_folder = root / "proj" / "builds"
        (builds_folder / ".hidden").mkdir()
        (builds_folder / "empty-folder").mkdir()
        (builds_folder / "stray.txt").write_text("x")

        found = await builds.latest_build("proj")
        assert found is not None and found.build_id == "b1"

    @pytest.mark.parametrize(
        "damage", [b"not gzip", b"", gzip.compress(b"{oops")], ids=["garbage", "empty", "bad-json"]
    )
    async def test_a_corrupt_record_is_infrastructure_unavailable(
        self, builds: FileBuildStore, root: Path, damage: bytes
    ) -> None:
        await builds.create_build(record())
        (root / "proj/builds/b1/build.json.gz").write_bytes(damage)

        with pytest.raises(InfrastructureUnavailableError) as raised:
            await builds.get_build("proj", "b1")
        assert raised.value.code == "file_store_unreadable"
        with pytest.raises(InfrastructureUnavailableError):
            await builds.latest_build("proj")

    async def test_a_corrupt_artifact_is_not_a_silent_miss(self, builds: FileBuildStore, root: Path) -> None:
        await builds.create_build(record())
        await builds.put_artifact("proj", "b1", "plan", {"v": 1})
        (root / "proj/builds/b1/plan.json.gz").write_bytes(b"garbage")
        with pytest.raises(InfrastructureUnavailableError):
            await builds.get_artifact("proj", "b1", "plan")

    async def test_the_same_artifact_is_written_as_the_same_bytes(self, tmp_path: Path) -> None:
        one, two = FileBuildStore(tmp_path / "one"), FileBuildStore(tmp_path / "two")
        for store in (one, two):
            await store.create_build(record())
            await store.put_artifact("proj", "b1", "plan", {"b": 1, "a": [1, 2]})
        assert (tmp_path / "one/proj/builds/b1/plan.json.gz").read_bytes() == (
            tmp_path / "two/proj/builds/b1/plan.json.gz"
        ).read_bytes()

    async def test_records_survive_a_new_store_object(self, builds: FileBuildStore, root: Path) -> None:
        await builds.create_build(record())
        await builds.put_progress("proj", "b1", "plan", "done")
        await builds.finish_build(result_of())

        reopened = FileBuildStore(root)
        stored = await reopened.get_build("proj", "b1")
        assert stored is not None and stored.finished and stored.progress == {"plan": "done"}
        assert await reopened.get_result("proj", "b1") == result_of()

    async def test_a_write_failure_is_infrastructure_unavailable(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocked"
        blocker.write_text("a file where the root should be")
        store = FileBuildStore(blocker)
        with pytest.raises(InfrastructureUnavailableError):
            await store.create_build(record())

    async def test_latest_build_of_a_project_without_builds_does_not_create_folders(
        self, builds: FileBuildStore, root: Path
    ) -> None:
        assert await builds.latest_build("proj") is None
        assert not await asyncio.to_thread(root.exists)


# open_file_stores and atomic writes ------------------------------------------------------------------


def test_open_file_stores_wires_every_port(tmp_path: Path) -> None:
    stores = open_file_stores(tmp_path / "root")

    assert isinstance(stores, FileStores)
    assert isinstance(stores.registry, FileSpecIdRegistry)
    assert isinstance(stores.extraction, FileExtractionStore)
    assert isinstance(stores.builds, FileBuildStore)
    assert stores.corpus is None
    assert not (tmp_path / "root").exists()  # opening writes nothing


def test_open_file_stores_with_a_corpus_folder_wires_the_corpus_source(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    stores = open_file_stores(tmp_path / "root", tmp_path / "docs")
    assert isinstance(stores.corpus, FileCorpusSource)


def test_every_file_store_has_the_methods_of_its_port(tmp_path: Path) -> None:
    stores = open_file_stores(tmp_path, tmp_path)
    expected = {
        "registry": ("taken", "record"),
        "extraction": ("get", "put", "get_many", "put_many"),
        "builds": (
            "create_build",
            "get_build",
            "latest_build",
            "put_progress",
            "finish_build",
            "get_result",
            "put_artifact",
            "get_artifact",
        ),
        "corpus": ("snapshot",),
    }
    for name, methods in expected.items():
        for method in methods:
            assert asyncio.iscoroutinefunction(getattr(getattr(stores, name), method)), (name, method)


def test_a_str_root_is_accepted(tmp_path: Path) -> None:
    stores = open_file_stores(str(tmp_path))
    assert isinstance(stores.builds, FileBuildStore)


async def test_the_stores_share_one_root_without_colliding(tmp_path: Path) -> None:
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "a.md").write_text("# A\n", encoding="utf-8")
    root = tmp_path / "root"
    stores = open_file_stores(root, corpus)
    assert stores.corpus is not None

    await stores.registry.record("proj", "b1", [SpecIdRow("MOD::A", "Module|surface|a", "uuid-1", "active")])
    await stores.extraction.put("nodes", "proj|f", FINGERPRINT, {"v": 1})
    await stores.builds.create_build(record())
    await stores.builds.put_artifact("proj", "b1", "plan", {"v": 1})
    await stores.corpus.snapshot("proj")

    assert all_files(root) == [
        "_extraction/nodes/" + next((root / "_extraction" / "nodes").iterdir()).name,
        "proj/builds/b1/build.json.gz",
        "proj/builds/b1/plan.json.gz",
        "proj/corpus-index.json",
        "proj/spec-ids.json",
    ]
    assert set(await stores.registry.taken("proj")) == {"MOD::A"}
    assert await stores.extraction.get("nodes", "proj|f", FINGERPRINT) == {"v": 1}


def test_the_registry_port_is_the_core_one(tmp_path: Path) -> None:
    registry: SpecIdRegistry = open_file_stores(tmp_path).registry
    assert hasattr(registry, "taken") and hasattr(registry, "record")


@dataclass
class UsedStores:
    root: Path
    stores: FileStores


async def use_every_store(root: Path) -> UsedStores:
    corpus = root.parent / "docs"
    corpus.mkdir(exist_ok=True)
    (corpus / "a.md").write_text("# A\n", encoding="utf-8")
    stores = open_file_stores(root, corpus)
    assert stores.corpus is not None
    await stores.registry.record("proj", "b1", [SpecIdRow("MOD::A", "Module|surface|a", "u", "active")])
    for index in range(3):
        await stores.extraction.put("nodes", f"k{index}", FINGERPRINT, {"v": index})
    await stores.extraction.put_many("vectors", FINGERPRINT, {f"v{index}": {"v": index} for index in range(5)})
    await stores.builds.create_build(record())
    await stores.builds.put_progress("proj", "b1", "plan", "running")
    await stores.builds.put_artifact("proj", "b1", "plan", {"v": 1})
    await stores.builds.put_artifact("proj", "b1", "plan", {"v": 2})
    await stores.builds.finish_build(result_of())
    await stores.corpus.snapshot("proj")
    (corpus / "a.md").write_text("# A edited\n", encoding="utf-8")
    await stores.corpus.snapshot("proj")
    return UsedStores(root, stores)


async def test_no_temporary_file_is_left_after_normal_use(tmp_path: Path) -> None:
    used = await use_every_store(tmp_path / "root")
    assert temporary_files(used.root) == []
    assert all_files(used.root)


async def test_no_temporary_file_is_left_after_concurrent_writes(tmp_path: Path) -> None:
    stores = open_file_stores(tmp_path)
    await stores.builds.create_build(record())
    await asyncio.gather(
        *(stores.builds.put_artifact("proj", "b1", "plan", {"v": index}) for index in range(20)),
        *(stores.extraction.put("nodes", f"k{index % 5}", FINGERPRINT, {"v": index}) for index in range(20)),
        *(stores.builds.put_progress("proj", "b1", STEPS[index % len(STEPS)], "done") for index in range(20)),
    )
    assert temporary_files(tmp_path) == []
    artifact = await stores.builds.get_artifact("proj", "b1", "plan")
    assert artifact is not None and artifact["v"] in range(20)  # one whole write won


async def test_a_failed_rename_leaves_no_temporary_file_and_the_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = open_file_stores(tmp_path)
    await stores.builds.create_build(record())
    await stores.builds.put_artifact("proj", "b1", "plan", {"v": "old"})
    await stores.extraction.put("nodes", "k", FINGERPRINT, {"v": "old"})
    real_replace = os.replace

    def broken(source: Any, target: Any) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(InfrastructureUnavailableError) as artifact_error:
        await stores.builds.put_artifact("proj", "b1", "plan", {"v": "new"})
    with pytest.raises(InfrastructureUnavailableError) as single_error:
        await stores.extraction.put("nodes", "k", FINGERPRINT, {"v": "new"})
    with pytest.raises(InfrastructureUnavailableError) as batch_error:
        await stores.extraction.put_many("nodes", FINGERPRINT, {"k": {"v": "new"}, "other": {"v": 1}})
    monkeypatch.setattr(os, "replace", real_replace)

    assert artifact_error.value.code == single_error.value.code == batch_error.value.code == "file_store_unwritable"
    assert temporary_files(tmp_path) == []
    assert await stores.builds.get_artifact("proj", "b1", "plan") == {"v": "old"}
    assert await stores.extraction.get("nodes", "k", FINGERPRINT) == {"v": "old"}
    assert await stores.extraction.get("nodes", "other", FINGERPRINT) is None


async def test_a_failed_corpus_index_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
    source = FileCorpusSource(tmp_path / "docs", tmp_path / "root")

    def broken(source: Any, target: Any) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(InfrastructureUnavailableError):
        await source.snapshot("proj")
    monkeypatch.undo()

    assert temporary_files(tmp_path / "root") == []
    assert not (tmp_path / "root" / "proj" / "corpus-index.json").exists()
    assert by_lineage(await source.snapshot("proj"))["a.md"].version == 1  # the failed run left no trace


def test_the_batch_writer_cleans_up_when_an_entry_cannot_be_written(tmp_path: Path) -> None:
    items: Mapping[str, bytes] = {"ok.bin": b"x"}
    target = tmp_path / "folder"
    target.mkdir()
    blocked = target / "child"
    blocked.mkdir()  # a directory where the final file should be: the rename fails
    with pytest.raises(InfrastructureUnavailableError):
        files_module._write_batch([(tmp_path / "ok.bin", items["ok.bin"]), (blocked, b"y")])
    assert temporary_files(tmp_path) == []
    assert (tmp_path / "ok.bin").read_bytes() == b"x"
