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
    @functools.wraps(handler)
    def wrapper(args: argparse.Namespace) -> int:
        try:
            return handler(args)
        except (ValueError, OSError) as exc:
            _emit({"error": str(exc)}, getattr(args, "json", False), str(exc))
            return EXIT_USAGE

    return wrapper

def _layout(args: argparse.Namespace) -> artifacts.Layout:
    root = local_env.resolve_project_root(getattr(args, "root_path", None))
    return artifacts.layout_for(root / args.outputs)

def _round_context(args: argparse.Namespace) -> tuple[artifacts.Layout, int]:
    layout = _layout(args)
    number = args.round or layout.latest_round()
    if number is None:
        raise ValueError("no rounds found — run new-round first")
    return layout, number

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

def cmd_round(args: argparse.Namespace) -> int:
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
            "matrix_unanswerable": sum(len(m.unanswerable) for m in result.matrices),
            "matrix_skipped": sum(len(m.missing) for m in result.matrices),
        },
        "disagreements": [d.as_dict() for d in result.disagreements],
        "blockers": open_blockers,
        "coverage": coverage.as_dict() if coverage else None,
        "matrices": [m.as_dict() for m in result.matrices],
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

    if payload.get("matrices"):
        lines.append("Each lens's own matrix:")
        for matrix in payload["matrices"]:
            lines.append(
                f"  {matrix['lens']} · {matrix['name']} — "
                f"{matrix['answered']}/{matrix['declared']} answered"
                + (f", {matrix['guessed']} guessed" if matrix["guessed"] else "")
            )
            for cell in matrix["unanswerable"]:
                lines.append(
                    f"      spec cannot say  {cell['row']} × {cell['col']}"
                    f"  — {cell['why']}"
                )
            for cell in matrix["missing"]:
                lines.append(f"      never filled     {cell['row']} × {cell['col']}")
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

    if counts["matrix_unanswerable"] or counts["matrix_skipped"]:
        lines.append(
            f"{counts['matrix_unanswerable']} matrix cell(s) a lens reached and "
            f"reported the spec cannot answer; {counts['matrix_skipped']} it "
            "enumerated and never came back to. The first is a finding, the second "
            "is an incomplete reading — re-run that lens."
        )

    if payload["incomplete_readings"]:
        lines.append(
            f"{len(payload['incomplete_readings'])} of {payload['readings_total']} "
            "readings could not be compared, so this round saw less than a whole "
            "one. Fix those and re-run before reading anything into the counts."
        )
    return "\n".join(lines)

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

def cmd_status(args: argparse.Namespace) -> int:
    layout = _layout(args)
    findings = artifacts.load_findings(layout)
    resolutions = artifacts.load_resolutions(layout)

    resolved = artifacts.resolved_ids(layout)
    open_blockers = [
        b for b in findings.get("blockers", []) if b.get("id") not in resolved
    ]
    counts = dict(findings.get("counts", {}))
    counts["open"] = len(open_blockers)

    payload = {
        "rounds_run": len(layout.rounds()),
        "resolved": len(resolutions),
        "resolutions": resolutions,
        "counts": counts,
        "blockers": open_blockers,
        "disagreements": findings.get("disagreements", []),
        "coverage": findings.get("coverage"),
        "matrices": findings.get("matrices", []),
    }

    lines = [
        f"Rounds run       {payload['rounds_run']}",
        f"Resolved         {payload['resolved']}",
        f"Open blockers    {payload['counts'].get('open', 0)}",
        f"Disagreements    {payload['counts'].get('disagreements', 0)}",
        f"Unanswered       {payload['counts'].get('uncovered_cells', 0)} grid cell(s)",
        f"Agreed guesses   {payload['counts'].get('agreed_guesses', 0)}",
        f"Spec cannot say  {payload['counts'].get('matrix_unanswerable', 0)} matrix cell(s)",
        f"Lens skipped     {payload['counts'].get('matrix_skipped', 0)} matrix cell(s)",
    ]
    if payload["blockers"]:
        lines.append("")
        lines.append("Still open:")
        lines += [
            f"  {b['id']} — {b.get('title', '')}" for b in payload["blockers"]
        ]
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK

def register(subparsers: argparse._SubParsersAction) -> None:
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
