"""Golden data of the graph stores: files recorded from Neo4j, and the helpers that compare a run with them.

``tests/fixtures/graph/golden_live/`` holds what the scripted live scenarios (``graph_probes.scenarios``)
produce on Neo4j:

- ``snapshots/<scenario>.json``: the canonical snapshot (``GraphProbe.canonical_snapshot``) after each
  scenario, with a ``meta`` of what the library reported;
- ``search_rankings.json``: the rankings of the shop queries through ``GraphReader`` (fulltext, the
  fused path with the exact vector walk and through the vector index).

Any store's live run is compared with them by ``snapshot_differences`` (equal graphs: no differences)
and ``compare_rankings`` (design D8: the same top-5 set per query; rank differences are listed, not
failures), so a milestone needs no running Neo4j. The files are written, byte-stable (sorted keys), only
by ``write_snapshot`` / ``write_rankings``, which refuse any store but Neo4j.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

GOLDEN_DIR: Final = Path(__file__).resolve().parents[1] / "fixtures" / "graph" / "golden_live"
SNAPSHOT_DIR: Final = GOLDEN_DIR / "snapshots"
RANKINGS_FILE: Final = GOLDEN_DIR / "search_rankings.json"
REFERENCE_BACKEND: Final = "neo4j"
TOP: Final = 5


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _lines(value: Any, depth: int, pretty: int) -> str:
    """``value`` one container level per line down to ``pretty`` levels, each deeper value on one line."""

    if depth >= pretty or not isinstance(value, (dict, list)) or not value:
        return _compact(value)
    pad, inner = "  " * depth, "  " * (depth + 1)
    if isinstance(value, dict):
        items = [f"{inner}{_compact(key)}: {_lines(value[key], depth + 1, pretty)}" for key in sorted(value)]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    return "[\n" + ",\n".join(f"{inner}{_lines(item, depth + 1, pretty)}" for item in value) + f"\n{pad}]"


def dump_json(data: Any, *, pretty: int = 2) -> str:
    """The one text form of golden data: sorted keys, the top ``pretty`` container levels one entry per
    line (a snapshot is one node or edge per line), the rest compact, UTF-8 text, final newline."""

    return _lines(data, 0, pretty) + "\n"


def _require_reference(backend: str) -> None:
    if backend != REFERENCE_BACKEND:
        raise PermissionError(f"golden data is recorded from {REFERENCE_BACKEND}, not {backend}")


def snapshot_path(scenario: str) -> Path:
    return SNAPSHOT_DIR / f"{scenario}.json"


def load_snapshot(scenario: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(snapshot_path(scenario).read_text(encoding="utf-8"))
    return data


def write_snapshot(backend: str, scenario: str, snapshot: Mapping[str, Any], meta: Mapping[str, Any]) -> Path:
    _require_reference(backend)
    path = snapshot_path(scenario)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_json({"scenario": scenario, "meta": meta, **snapshot}), encoding="utf-8")
    return path


def load_rankings() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(RANKINGS_FILE.read_text(encoding="utf-8"))
    return data


def write_rankings(backend: str, rankings: Mapping[str, Any]) -> Path:
    _require_reference(backend)
    RANKINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RANKINGS_FILE.write_text(dump_json(rankings, pretty=3), encoding="utf-8")
    return RANKINGS_FILE


def _key(entry: Mapping[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, ensure_ascii=False)


def _identity(kind: str, entry: Mapping[str, Any]) -> str:
    if kind == "nodes":
        return f"{entry.get('spec_id')}|{'/'.join(entry['labels'])}"
    return f"{entry['source']}|{entry['type']}|{entry['target']}|{entry.get('edge_key')}"


def _changed(want: Mapping[str, Any], have: Mapping[str, Any]) -> list[str]:
    a, b = want["props"], have["props"]
    return sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))


def snapshot_differences(expected: Mapping[str, Any], actual: Mapping[str, Any], *, limit: int = 20) -> list[str]:
    """Human-readable differences between two canonical snapshots (empty when equal).

    Nodes are paired by spec id and labels, edges by source, type, target and edge key; a pair whose
    properties differ names the properties. At most ``limit`` lines are returned, then a count.
    """

    lines: list[str] = []
    for kind in ("nodes", "edges"):
        want = {_identity(kind, e): e for e in expected[kind]}
        have = {_identity(kind, e): e for e in actual[kind]}
        if len(want) != len(expected[kind]) or len(have) != len(actual[kind]):
            lines.append(f"{kind}: identities are not unique")
        lines.extend(f"{kind}: missing {name}" for name in sorted(want.keys() - have.keys()))
        lines.extend(f"{kind}: unexpected {name}" for name in sorted(have.keys() - want.keys()))
        lines.extend(
            f"{kind}: {name} differs in {_changed(want[name], have[name]) or 'labels or ends'}"
            for name in sorted(want.keys() & have.keys())
            if _key(want[name]) != _key(have[name])
        )
    if len(lines) > limit:
        lines = [*lines[:limit], f"... and {len(lines) - limit} more"]
    return lines


@dataclass
class RankingParity:
    """The outcome of comparing rankings: set mismatches fail parity, rank differences are reported."""

    queries: int = 0
    set_mismatches: list[str] = field(default_factory=list)
    rank_differences: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.set_mismatches

    def report(self) -> str:
        return "\n".join(
            [f"{self.queries} rankings compared"]
            + [f"SET   {line}" for line in self.set_mismatches]
            + [f"RANK  {line}" for line in self.rank_differences]
        )


def _ids(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    return [e["spec_id"] if "spec_id" in e else e["fragment_id"] for e in entries]


def compare_rankings(
    expected: Mapping[str, Any], actual: Mapping[str, Any], *, top: int = TOP, tolerance: int = 0
) -> RankingParity:
    """Per query and mode: the first ``top`` ids must be the same set, up to ``tolerance`` ids that the other
    run has instead (else a set mismatch); a different order, or a different tail, is a rank difference.
    ``tolerance`` is for a store whose text scoring is not Lucene's (a documented, per-store allowance; 0
    for Neo4j). Queries and modes must be the same."""

    parity = RankingParity()
    if sorted(expected["queries"]) != sorted(actual["queries"]):
        parity.set_mismatches.append("the two runs ask different queries")
        return parity
    for text, want_entry in expected["queries"].items():
        have_entry = actual["queries"][text]
        for mode in want_entry["modes"]:
            parity.queries += 1
            want, have = _ids(want_entry[mode]), _ids(have_entry[mode])
            if len(set(want[:top]) - set(have[:top])) > tolerance:
                parity.set_mismatches.append(
                    f"{mode} {text!r}: top {top} {sorted(set(want[:top]) ^ set(have[:top]))} not in both"
                )
            elif want != have:
                parity.rank_differences.append(f"{mode} {text!r}: {want} -> {have}")
    return parity
