"""``specflow refine`` — the spec-refinement command group.

Four commands, and each one exists because a model doing the same job by eye
would be *less* reliable, not more:

  new-round  derives the round directory and file names, so prose never spells
             out a path that stops honouring --outputs
  round      compares the readings and checks them against the round's grid —
             list comparison over items too numerous to track by hand
  resolve    appends a decision to a cumulative file, so later rounds stop
             re-asking it
  status     reads the last round's findings back, minus anything since resolved

There is deliberately no validate, no completeness gate, no score, and **no stop
rule**. Whether a reading is good, whether the spec is ready, whether a decision
is worth a human's attention, and whether another round is worth running are all
judgments; the skill makes them in the open where the user can disagree. An
earlier version diffed each round against the previous ones and the skill read
"nothing new" as convergence — see ``refine_compare`` for why that inference does
not hold and what was removed with it.

Every command is **local** — it reads and writes files under the project's
outputs directory and touches no backend, which is why ``cli.main`` dispatches
the group without resolving a backend URL or running the localhost guard.

Exit codes: 0 success, 2 bad usage. Nothing here fails a run on a judgment call.
The second half of that is enforced by ``_reporting_usage_errors``, which wraps
every handler registered below — the promise is made in this module's help text
and the plugin README, so it is kept here rather than left to the host CLI's
generic dispatch, which catches nothing of the sort.
"""

from __future__ import annotations

import argparse
import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from services import local_env
from services import refine_artifacts as artifacts
from services import refine_compare as compare

EXIT_OK, EXIT_USAGE = 0, 2


def _emit(payload: dict[str, Any], as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(human)


def _reporting_usage_errors(
    handler: Callable[[argparse.Namespace], int],
) -> Callable[[argparse.Namespace], int]:
    """Turn a bad-input error into the documented message and exit code 2.

    Every failure these commands have is a bad input: no rounds yet, a round with
    no readings, a file that is not the JSON it should be. The likeliest one in
    practice is a lens subagent that never wrote its file, which the loop hits
    mid-round — so it has to read as a message about a file, not as a traceback
    out of ``main``.

    Reported through ``_emit`` on stdout, like ``cmd_resolve``'s own refusal, so
    ``--json`` callers still get parseable JSON and a skill capturing stdout
    still sees what went wrong.
    """

    @functools.wraps(handler)
    def wrapper(args: argparse.Namespace) -> int:
        try:
            return handler(args)
        except (ValueError, OSError) as exc:
            _emit({"error": str(exc)}, getattr(args, "json", False), str(exc))
            return EXIT_USAGE

    return wrapper


def _layout(args: argparse.Namespace) -> artifacts.Layout:
    """Resolve ``--outputs`` against the project root, honouring ``--root-path``.

    The host CLI's global ``--root-path`` reaches these commands too. Resolving
    against the process cwd instead meant `--root-path /proj refine status`
    silently read `./docs`, reported no rounds, and a later `resolve` wrote its
    artifacts into whichever tree the shell happened to be in.
    """
    root = local_env.resolve_project_root(getattr(args, "root_path", None))
    return artifacts.layout_for(root / args.outputs)


def _round_context(args: argparse.Namespace) -> tuple[artifacts.Layout, int]:
    """Resolve --outputs/--round for the commands that operate on a round.

    Raises rather than emitting: ``_reporting_usage_errors`` turns a ValueError
    into the same message with EXIT_USAGE.
    """
    layout = _layout(args)
    number = args.round or layout.latest_round()
    if number is None:
        raise ValueError("no rounds found — run new-round first")
    return layout, number


# ---------------------------------------------------------------- new-round

def cmd_new_round(args: argparse.Namespace) -> int:
    layout = _layout(args)
    number = (layout.latest_round() or 0) + 1
    directory = layout.round_dir(number)
    directory.mkdir(parents=True, exist_ok=True)

    write_to = [str(layout.reading_path(number, lens)) for lens in args.lens]
    payload = {
        "round": number,
        "dir": str(directory),
        "write_to": write_to,
        "grid": str(layout.grid_path(number)),
        "coherence": str(layout.coherence_path(number)),
    }
    lines = [f"Round {number} -> {directory}"]
    lines.append(f"  grid     {artifacts.GRID_FILE}  (write first; every lens fills it)")
    lines += [f"  expects  {Path(p).name}" for p in write_to]
    lines.append(f"  then     {artifacts.COHERENCE_FILE}  (optional)")
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


# ---------------------------------------------------------------- round

def cmd_round(args: argparse.Namespace) -> int:
    """Compare this round's readings and say what is new since the last one."""
    layout, number = _round_context(args)

    readings = artifacts.load_readings(layout, number)
    if not readings:
        raise ValueError(
            f"no readings in {layout.round_dir(number)} — "
            f"each lens writes {artifacts.READING_PREFIX}<lens>.json there"
        )

    result = compare.compare(
        readings,
        grid=artifacts.load_grid(layout, number),
        coherence=artifacts.load_coherence(layout, number),
    )
    if not result.lens_count:
        raise ValueError(
            f"none of the {result.readings_total} reading(s) in "
            f"{layout.round_dir(number)} could be compared:\n  "
            + "\n  ".join(result.incomplete)
        )

    resolved = artifacts.resolved_ids(layout)
    open_blockers = [
        b for b in result.blockers if b.get("id") not in resolved
    ]

    coverage = result.coverage
    payload = {
        "round": number,
        "lenses": result.lenses,
        "lens_count": result.lens_count,
        "readings_total": result.readings_total,
        "counts": {
            "open": len(open_blockers),
            "already_decided": len(result.blockers) - len(open_blockers),
            "disagreements": len(result.disagreements),
            "uncovered_cells": len(coverage.uncovered) if coverage else 0,
            "agreed_guesses": len(coverage.agreed_guesses) if coverage else 0,
        },
        "disagreements": [d.as_dict() for d in result.disagreements],
        "blockers": open_blockers,
        "coverage": coverage.as_dict() if coverage else None,
        "incomplete_readings": result.incomplete,
        "notes": result.notes,
        "findings_path": str(layout.findings_path),
    }

    artifacts.write_json(layout.findings_path, payload)

    _emit(payload, args.json, _render_round(payload))
    return EXIT_OK


def _render_round(payload: dict[str, Any]) -> str:
    counts = payload["counts"]
    compared, total = payload["lens_count"], payload["readings_total"]
    # Naming the shortfall in the headline, not only in the list below it: the
    # header is what gets quoted back to the user, and "6 lenses" over four
    # comparable readings overstates the round's only evidence.
    head = (
        f"{compared} lenses"
        if compared == total
        else f"{compared} of {total} readings compared"
    )
    lines = [
        f"Round {payload['round']} — {head}: {', '.join(payload['lenses'])}",
        f"  open {counts['open']}   already decided {counts['already_decided']}   "
        f"disagreements {counts['disagreements']}",
        "",
    ]

    if payload["incomplete_readings"]:
        lines.append("Readings that could not be compared:")
        lines += [f"  {item}" for item in payload["incomplete_readings"]]
        lines.append("")

    if payload["notes"]:
        lines.append("Worth knowing about this round:")
        lines += [f"  {item}" for item in payload["notes"]]
        lines.append("")

    coverage = payload.get("coverage")
    if coverage:
        lines.append(
            f"Grid: {coverage['cells_filled']}/{coverage['cells_total']} cells "
            "answered by at least one lens"
        )
        if coverage["uncovered"]:
            lines.append("  no lens answered these:")
            lines += [
                f"    {cell['id']} — {cell['question']}"
                for cell in coverage["uncovered"]
            ]
        if coverage["agreed_guesses"]:
            lines.append("  agreed, but every lens was guessing:")
            lines += [
                f"    {cell['id']} — {cell['value']}  "
                f"({', '.join(cell['lenses'])})"
                for cell in coverage["agreed_guesses"]
            ]
        lines.append("")

    if payload["blockers"]:
        lines.append("Open blockers:")
        for blocker in payload["blockers"]:
            found = ", ".join(blocker.get("found_by", [])) or "unknown"
            lines.append(f"  {blocker['id']} — {blocker.get('title', '')}")
            lines.append(
                f"      raised by {found}"
                + (f"  in {blocker['where']}" if blocker.get("where") else "")
            )
            if blocker.get("question"):
                lines.append(f"      {blocker['question']}")
            # The options are the decision. Without them a blocker synthesized
            # from a disagreement printed the question and neither answer, which
            # is the one thing the round actually established.
            for option in blocker.get("options", []):
                consequence = option.get("consequence")
                lines.append(
                    f"        - {option.get('label', '?')}"
                    + (f" — {consequence}" if consequence else "")
                )
            for item in blocker.get("disagreements", []):
                lines.append(f"      readings disagree — {item['question']}")
                phrasings = item.get("phrasings", {})
                for lens, answer in item["answers"].items():
                    lines.append(f"          {lens}: {answer}")
                    # Only present when the lenses worded it differently. Shown
                    # so an answer is never read against someone else's wording.
                    asked = phrasings.get(lens)
                    if asked and asked != item["question"]:
                        lines.append(f"              asked as: {asked}")
        lines.append("")

    if counts["uncovered_cells"] or counts["agreed_guesses"]:
        lines.append(
            f"{counts['uncovered_cells']} cell(s) no lens answered and "
            f"{counts['agreed_guesses']} answered only by agreeing guesses. "
            "Neither shows up as a disagreement."
        )

    # No verdict on whether the round settled anything, and no count of what is
    # "new" since the last one. Both existed here, and both were read as evidence
    # the spec had converged — an inference nothing in this data supports.
    if payload["incomplete_readings"]:
        lines.append(
            f"{len(payload['incomplete_readings'])} of {payload['readings_total']} "
            "readings could not be compared, so this round saw less than a whole "
            "one. Fix those and re-run before reading anything into the counts."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- resolve

def cmd_resolve(args: argparse.Namespace) -> int:
    layout = _layout(args)
    existing = artifacts.load_resolutions(layout)
    if any(r.get("blocker_id") == args.id for r in existing):
        _emit(
            {"error": "already resolved", "blocker_id": args.id},
            args.json,
            f"{args.id} is already resolved.",
        )
        return EXIT_USAGE

    record = {
        "blocker_id": args.id,
        "choice": args.choice,
        "applied_to_spec": args.applied_to or [],
        "source": args.source,
    }
    existing.append(record)
    artifacts.write_json(layout.resolutions_path, {"resolved": existing})

    _emit(
        {"recorded": record, "total_resolved": len(existing)},
        args.json,
        f"Recorded {args.id} -> {args.choice}  ({len(existing)} resolved in total)",
    )
    return EXIT_OK


# ---------------------------------------------------------------- status

def cmd_status(args: argparse.Namespace) -> int:
    layout = _layout(args)
    findings = artifacts.load_findings(layout)
    resolutions = artifacts.load_resolutions(layout)

    # `findings.json` is a snapshot from the last `round`; the resolutions file
    # keeps moving after it. Re-subtracting here rather than reporting the
    # snapshot's own count, because `/specflow-resolve` reads this immediately
    # after recording a decision — and a decision the user just made showing up as
    # still open is how the loop re-asks something it was told to stop asking.
    resolved = artifacts.resolved_ids(layout)
    open_blockers = [
        b for b in findings.get("blockers", []) if b.get("id") not in resolved
    ]
    counts = dict(findings.get("counts", {}))
    counts["open"] = len(open_blockers)

    payload = {
        # Counted from the directories on disk. There is no separate round ledger
        # — the one that existed only fed a convergence diff this design dropped.
        "rounds_run": len(layout.rounds()),
        "resolved": len(resolutions),
        "resolutions": resolutions,
        "counts": counts,
        "blockers": open_blockers,
        "disagreements": findings.get("disagreements", []),
        "coverage": findings.get("coverage"),
    }

    lines = [
        f"Rounds run      {payload['rounds_run']}",
        f"Resolved        {payload['resolved']}",
        f"Open blockers   {payload['counts'].get('open', 0)}",
        f"Disagreements   {payload['counts'].get('disagreements', 0)}",
        f"Unanswered      {payload['counts'].get('uncovered_cells', 0)} grid cell(s)",
        f"Agreed guesses  {payload['counts'].get('agreed_guesses', 0)}",
    ]
    if payload["blockers"]:
        lines.append("")
        lines.append("Still open:")
        lines += [
            f"  {b['id']} — {b.get('title', '')}" for b in payload["blockers"]
        ]
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


# ---------------------------------------------------------------- parser

def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``refine`` group to the host CLI's subparsers.

    Nested rather than flat: ``status`` and ``resolve`` are far too generic at
    the top level, and ``status`` would sit confusingly next to the existing
    ``check-status``, which reports on something else entirely.
    """
    refine = subparsers.add_parser(
        "refine",
        help="Spec refinement — compare independent readings of a spec",
        description=(
            "Local spec refinement. Independent lenses read the spec; these "
            "commands compare what they disagree about and remember what you "
            "have already decided."
        ),
    )
    commands = refine.add_subparsers(dest="refine_command", metavar="COMMAND")
    commands.required = True

    def leaf(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--outputs", default="docs", help="outputs dir (default: docs)")
        sub.add_argument("--json", action="store_true", help="machine-readable output")
        return sub

    # Wrapped once, here, rather than at each handler: the exit-code promise
    # belongs to the group, and a handler added later inherits it by being
    # registered instead of by remembering a decorator.
    def bind(parser: argparse.ArgumentParser, handler: Any) -> None:
        parser.set_defaults(func=_reporting_usage_errors(handler))

    new_round = leaf("new-round", "allocate the next round directory")
    new_round.add_argument("--lens", nargs="*", default=[], help="lens names this round")
    bind(new_round, cmd_new_round)

    round_cmd = leaf("round", "compare this round's readings")
    round_cmd.add_argument("--round", type=int)
    bind(round_cmd, cmd_round)

    resolve = leaf("resolve", "record a decision")
    resolve.add_argument("--id", required=True, help="blocker id")
    resolve.add_argument("--choice", required=True, help="the chosen option label")
    resolve.add_argument("--applied-to", nargs="*", help="spec files updated")
    resolve.add_argument(
        "--source", default="user", choices=["user", "assumed"],
        help="whether the user decided or the skill applied a default",
    )
    bind(resolve, cmd_resolve)

    bind(leaf("status", "current refinement state"), cmd_status)
