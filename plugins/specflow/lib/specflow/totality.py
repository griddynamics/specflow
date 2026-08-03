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
from typing import Any

# Values that look filled but say nothing. An agent that cannot determine a
# value must raise a blocker, not shrug in the cell.
EVASIONS = frozenset({
    "", "-", "--", "?", "??", "n/a", "na", "none", "tbd", "todo", "tbc",
    "unknown", "unclear", "unspecified", "not specified", "not defined",
    "undefined", "any", "either", "varies", "depends", "flexible",
    "to be determined", "to be decided", "open question", "see spec",
})


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


def _blocker_anchors(interpretation: dict[str, Any]) -> set[tuple[str, str]]:
    """(file, section) pairs that have at least one blocker raised against them."""
    anchors = set()
    for blocker in interpretation.get("blockers", []):
        anchor = blocker.get("spec_anchor") or {}
        anchors.add((anchor.get("file", ""), anchor.get("section", "")))
    return anchors


def _walk_anchors(node: Any, path: str = ""):
    """Yield (path, anchor) for every spec_anchor in the tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key == "spec_anchor" and isinstance(value, dict):
                yield path or "<root>", value
            else:
                yield from _walk_anchors(value, child)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_anchors(item, f"{path}[{i}]")


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

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
        elif _is_evasion(node):
            report.add(path, f"{node!r} is an evasion, not a decision — raise a blocker instead")

    # Blocker text is allowed to discuss uncertainty; the rest of the artifact is not.
    for key, value in interpretation.items():
        if key in ("blockers", "_path"):
            continue
        walk(value, key)


def _check_state_matrices(interpretation: dict[str, Any], report: TotalityReport) -> None:
    """Every state x event pair needs an outcome. This is the core forcing function."""
    for i, machine in enumerate(interpretation.get("state_machines", [])):
        path = f"state_machines[{i}]"
        states = machine.get("states") or []
        events = machine.get("events") or []
        covered = {
            (row.get("state"), row.get("event"))
            for row in machine.get("matrix", [])
        }
        missing = [pair for pair in product(states, events) if pair not in covered]
        if missing:
            shown = ", ".join(f"{s} x {e}" for s, e in missing[:6])
            more = f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""
            report.add(
                path,
                f"{machine.get('entity', '?')} matrix is partial — "
                f"{len(missing)} uncovered pair(s): {shown}{more}",
            )
        unknown = [
            (row.get("state"), row.get("event"))
            for row in machine.get("matrix", [])
            if row.get("state") not in states or row.get("event") not in events
        ]
        for state, event in unknown:
            report.add(path, f"matrix row references undeclared state/event: {state} x {event}")


def _check_references(interpretation: dict[str, Any], report: TotalityReport) -> None:
    """Operations and foreign keys must point at entities that exist."""
    entities = {e.get("name") for e in interpretation.get("entities", [])}

    for i, operation in enumerate(interpretation.get("operations", [])):
        target = operation.get("entity")
        if target and target not in entities:
            report.add(
                f"operations[{i}]",
                f"'{operation.get('name')}' acts on unknown entity '{target}'",
            )

    for i, entity in enumerate(interpretation.get("entities", [])):
        for j, field_def in enumerate(entity.get("fields", [])):
            target = field_def.get("references")
            if target and target not in entities:
                report.add(
                    f"entities[{i}].fields[{j}]",
                    f"'{field_def.get('name')}' references unknown entity '{target}'",
                )

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

    def is_raised(anchor: dict[str, Any]) -> bool:
        key = (anchor.get("file", ""), anchor.get("section", ""))
        # Match on file+section, or fall back to file alone.
        return key in raised or any(f == key[0] for f, _ in raised)

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
