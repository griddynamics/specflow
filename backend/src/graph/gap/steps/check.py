"""Step ``check``: validate each investigation's report before anything is written (docs/graph/gap/investigation.md
"Checks").

SLICE-1 MINIMUM. Session 06 adds the duplicate check, the second check (role ``gap_judge``) and the option and edit
checks. Until then the step checks what the record step relies on:

- a failed investigation (error or no report) defers the site to the next run (FR-GRUN-04);
- ``not_a_gap``, ``not_spec`` and ``duplicate_of`` verdicts become those site outcomes;
- a ``gap`` needs a kind and a question; ``ABOUT`` and ``IMPACTS`` keep only nodes of the graph snapshot (an
  ``IMPACTS`` node that is also ``ABOUT`` is dropped); a witness quote is kept only when it is still found in its
  active fragment, a graph fact only when it names a seed of the site; title, question and why are cut to their
  limits. A gap left with no ``ABOUT`` node and no quote is deferred (data-model.md GI1), unless it is ``info`` (a
  note needs neither).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from graph.gap.identity import normalise_quote
from graph.gap.models import GapReport, InvestigationResult, WitnessRef
from graph.gap.names import SiteOutcome
from graph.gap.steps.snapshot import GraphSnapshot

TITLE_CHARS: Final = 160
QUESTION_CHARS: Final = 1000
WHY_CHARS: Final = 2000
_VERDICT_OUTCOMES: Final[dict[str, SiteOutcome]] = {
    "not_a_gap": "not_a_gap",
    "not_spec": "not_spec",
    "duplicate_of": "duplicate",
}


@dataclass(frozen=True)
class Checked:
    """A site's outcome after the check, the checked report (for ``gap`` and the other verdicts) and warning codes."""

    outcome: SiteOutcome
    report: GapReport | None = None
    warnings: tuple[str, ...] = ()


def _witness_holds(snapshot: GraphSnapshot, ref: WitnessRef, seeds: int) -> bool:
    if ref.fact is not None:
        return 0 <= ref.fact < seeds and ref.fragment_id is None and ref.quote is None
    if ref.fragment_id is None or not ref.quote:
        return False
    fragment = snapshot.fragments.get(ref.fragment_id)
    return fragment is not None and normalise_quote(ref.quote) in normalise_quote(fragment.text)


def check_result(result: InvestigationResult, snapshot: GraphSnapshot, *, seeds: int) -> Checked:
    """The checked outcome of one site's investigation result; ``seeds`` is the number of seeds of the site."""

    report = result.report
    if result.error is not None or report is None:
        return Checked("deferred", warnings=(result.error or "no_report",))
    if report.verdict != "gap":
        return Checked(_VERDICT_OUTCOMES[report.verdict], report)
    if report.kind is None or not report.question.strip():
        return Checked("deferred", warnings=("report_incomplete",))
    about = tuple(dict.fromkeys(spec_id for spec_id in report.about if spec_id in snapshot.nodes))
    impacts = tuple(
        dict.fromkeys(spec_id for spec_id in report.impacts if spec_id in snapshot.nodes and spec_id not in about)
    )
    witness = tuple(dict.fromkeys(ref for ref in report.witness if _witness_holds(snapshot, ref, seeds)))
    warnings = [
        code
        for code, dropped in (
            ("about_dropped", len(about) < len(set(report.about))),
            ("impacts_dropped", len(impacts) < len(set(report.impacts) - set(about))),
            ("witness_dropped", len(witness) < len(set(report.witness))),
        )
        if dropped
    ]
    if not about and not any(ref.quote for ref in witness) and report.severity != "info":
        return Checked("deferred", warnings=(*warnings, "gap_without_anchor"))
    checked = report.model_copy(
        update={
            "about": about,
            "impacts": impacts,
            "witness": witness,
            "title": report.title.strip()[:TITLE_CHARS],
            "question": report.question.strip()[:QUESTION_CHARS],
            "why": report.why.strip()[:WHY_CHARS],
        }
    )
    return Checked("gap", checked, tuple(warnings))
