"""Totality checks: the forcing function that replaces building.

A physical build compels decisions — you cannot run code past a point the spec
left undefined. Simulation has no such compulsion, so an agent asked for
blockers produces a *plausible* list rather than an exhaustive one, and the
awkward cases go unmentioned.

Totality restores the compulsion structurally. A prose list is partial by
nature; a filled matrix is total by construction. These checks enforce that:

  1. Every dimension carries a real value, not an evasion.
  2. Every state x event pair in a lifecycle has an outcome.
  3. Every reference resolves to something that exists.
  4. Every escape hatch is paid for with a blocker.

(4) is the one that matters most. An agent can always write ``inferred: true``
or ``outcome: "undefined_in_spec"`` to get past a gap — those are legitimate
answers, but only if the gap is also *raised*. Without this check the escape
hatches become a silent way to skip the hard cells, which is exactly the failure
mode simulation is prone to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterable, NamedTuple

from . import tree

# Values that look filled but say nothing. An agent that cannot determine a
# value must raise a blocker, not shrug in the cell.
EVASIONS = frozenset({
    "", "-", "--", "?", "??", "n/a", "na", "none", "tbd", "todo", "tbc",
    "unknown", "unclear", "unspecified", "not specified", "not defined",
    "undefined", "any", "either", "varies", "depends", "flexible",
    "to be determined", "to be decided", "open question", "see spec",
})

# Blocker text is allowed to discuss uncertainty; the rest of an artifact is not.
# ``_path`` is bookkeeping added on load, not spec content.
_EVASION_EXEMPT = frozenset({"blockers", "_path"})


@dataclass
class Finding:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class TotalityReport:
    lens: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, path: str, message: str) -> None:
        self.findings.append(Finding(path, message))


def _is_evasion(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in EVASIONS


def evasion_findings(
    document: dict[str, Any], *, exempt: Iterable[str] = ()
) -> list[Finding]:
    """Every cell in ``document`` that looks filled but says nothing.

    Public because the same rule applies wherever dimensions are recorded — once
    embedded in a lens artifact, once standalone as ``analysis/dimensions.json``.
    Two implementations of "TBD is not an answer" would drift.
    """
    exempt = frozenset(exempt)
    findings: list[Finding] = []
    for key, value in document.items():
        if key in exempt:
            continue
        for node in tree.walk(value, key):
            if node.is_leaf and _is_evasion(node.value):
                findings.append(
                    Finding(
                        node.path,
                        f"{node.value!r} is an evasion, not a decision — raise a blocker instead",
                    )
                )
    return findings


class DanglingRef(NamedTuple):
    """A foreign key pointing at an entity the interpretation never defines."""

    path: str
    entity: str
    field: str
    target: str


def entity_names(interpretation: dict[str, Any]) -> set[str]:
    """The entity names an artifact defines. Every reference must land in here."""
    return {e.get("name") for e in interpretation.get("entities", [])}


def dangling_field_references(interpretation: dict[str, Any]) -> list[DanglingRef]:
    """Foreign keys with no target.

    Public because both oracles report this — totality as an unresolved
    reference, contracts as a structural defect. They word it differently on
    purpose; the predicate must not be written twice.
    """
    known = entity_names(interpretation)
    dangling = []
    for i, entity in enumerate(interpretation.get("entities", [])):
        for j, field_def in enumerate(entity.get("fields", [])):
            target = field_def.get("references")
            if target and target not in known:
                dangling.append(
                    DanglingRef(
                        path=f"entities[{i}].fields[{j}]",
                        entity=entity.get("name", "?"),
                        field=field_def.get("name", "?"),
                        target=target,
                    )
                )
    return dangling


def _blocker_anchors(interpretation: dict[str, Any]) -> set[tuple[str, str]]:
    """(file, section) pairs that have at least one blocker raised against them."""
    anchors = set()
    for blocker in interpretation.get("blockers", []):
        anchor = blocker.get("spec_anchor") or {}
        anchors.add((anchor.get("file", ""), anchor.get("section", "")))
    return anchors


def _walk_anchors(interpretation: dict[str, Any]):
    """Yield (owning element's path, anchor) for every spec_anchor in the tree."""
    for node in tree.walk(interpretation):
        if node.key == "spec_anchor" and isinstance(node.value, dict):
            yield node.owner or "<root>", node.value


def check(interpretation: dict[str, Any]) -> TotalityReport:
    """Run every totality check against one lens artifact."""
    report = TotalityReport(lens=interpretation.get("lens", "<unnamed>"))

    _check_no_evasions(interpretation, report)
    _check_state_matrices(interpretation, report)
    _check_references(interpretation, report)
    _check_escape_hatches(interpretation, report)
    _check_blocker_shape(interpretation, report)

    return report


def _check_no_evasions(interpretation: dict[str, Any], report: TotalityReport) -> None:
    """A filled-looking cell that says nothing is not filled."""
    report.findings.extend(evasion_findings(interpretation, exempt=_EVASION_EXEMPT))


def _check_state_matrices(interpretation: dict[str, Any], report: TotalityReport) -> None:
    """Every state x event pair needs an outcome. This is the core forcing function."""
    for i, machine in enumerate(interpretation.get("state_machines", [])):
        path = f"state_machines[{i}]"
        states = machine.get("states") or []
        events = machine.get("events") or []

        covered: set[tuple[Any, Any]] = set()
        for row in machine.get("matrix", []):
            pair = (row.get("state"), row.get("event"))
            covered.add(pair)
            if pair[0] not in states or pair[1] not in events:
                report.add(
                    path,
                    f"matrix row references undeclared state/event: {pair[0]} x {pair[1]}",
                )

        missing = [pair for pair in product(states, events) if pair not in covered]
        if missing:
            shown = ", ".join(f"{s} x {e}" for s, e in missing[:6])
            more = f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""
            report.add(
                path,
                f"{machine.get('entity', '?')} matrix is partial — "
                f"{len(missing)} uncovered pair(s): {shown}{more}",
            )


def _check_references(interpretation: dict[str, Any], report: TotalityReport) -> None:
    """Operations and foreign keys must point at entities that exist."""
    entities = entity_names(interpretation)

    for i, operation in enumerate(interpretation.get("operations", [])):
        target = operation.get("entity")
        if target and target not in entities:
            report.add(
                f"operations[{i}]",
                f"'{operation.get('name')}' acts on unknown entity '{target}'",
            )

    for ref in dangling_field_references(interpretation):
        report.add(ref.path, f"'{ref.field}' references unknown entity '{ref.target}'")

    for i, machine in enumerate(interpretation.get("state_machines", [])):
        target = machine.get("entity")
        if target and target not in entities:
            report.add(f"state_machines[{i}]", f"lifecycle for unknown entity '{target}'")


def _check_escape_hatches(interpretation: dict[str, Any], report: TotalityReport) -> None:
    """Every admitted gap must be raised as a blocker.

    Without this, ``inferred: true`` and ``outcome: "undefined_in_spec"`` become a
    quiet way to skip the hard cells while still passing every other check.
    """
    raised = _blocker_anchors(interpretation)
    # Indexed, not scanned: is_raised runs once per anchor in the artifact, which
    # is every entity, field, operation, machine and failure mode.
    raised_files = {file for file, _ in raised}

    def is_raised(anchor: dict[str, Any]) -> bool:
        # Match on file+section, or fall back to file alone.
        return (anchor.get("file", ""), anchor.get("section", "")) in raised or (
            anchor.get("file", "") in raised_files
        )

    for path, anchor in _walk_anchors(interpretation):
        if anchor.get("inferred") and not is_raised(anchor):
            report.add(
                path,
                "value is marked inferred but no blocker was raised against "
                f"{anchor.get('file')} — an admitted gap must be surfaced",
            )

    for i, machine in enumerate(interpretation.get("state_machines", [])):
        anchor = machine.get("spec_anchor") or {}
        for row in machine.get("matrix", []):
            if row.get("outcome") == "undefined_in_spec" and not is_raised(anchor):
                report.add(
                    f"state_machines[{i}]",
                    f"{row.get('state')} x {row.get('event')} is undefined in the spec "
                    "but no blocker was raised for it",
                )
                break

    for i, mode in enumerate(interpretation.get("failure_modes", [])):
        anchor = mode.get("spec_anchor") or {}
        if str(mode.get("spec_says", "")).strip().lower() in ("nothing", "silent", "unhandled"):
            if not is_raised(anchor):
                report.add(
                    f"failure_modes[{i}]",
                    f"'{mode.get('scenario')}' is unhandled by the spec but no blocker "
                    "was raised for it",
                )


def _check_blocker_shape(interpretation: dict[str, Any], report: TotalityReport) -> None:
    """A recommendation that is not one of the options cannot be applied."""
    for i, blocker in enumerate(interpretation.get("blockers", [])):
        labels = [o.get("label") for o in blocker.get("options", [])]
        recommended = blocker.get("recommended")
        if recommended and recommended not in labels:
            report.add(
                f"blockers[{i}]",
                f"recommended {recommended!r} is not one of the options {labels}",
            )
