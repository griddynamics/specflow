"""``specflow refine`` — the spec-refinement command group.

Four commands, and each one exists because a model doing the same job by eye
would be *less* reliable, not more:

  new-round  derives the round directory and file names, so prose never spells
             out a path that stops honouring --outputs
  round      compares the readings and diffs this round against every previous
             one — list comparison over items too numerous to track by hand
  resolve    appends a decision to a cumulative file, so later rounds stop
             re-asking it
  status     reads that state back for the reporting and planning skills

There is deliberately no validate, no completeness gate, and no score. Whether a
reading is good, whether the spec is ready, and whether a decision is worth a
human's attention are judgments; the skill makes them in the open where the user
can disagree. See ``refine_compare`` for the reasoning.

Every command is **local** — it reads and writes files under the project's
outputs directory and touches no backend, which is why ``cli.main`` dispatches
the group without resolving a backend URL or running the localhost guard.

Exit codes: 0 success, 2 bad usage. Nothing here fails a run on a judgment call.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from services import refine_artifacts as artifacts
from services import refine_compare as compare

EXIT_OK, EXIT_USAGE = 0, 2


def _emit(payload: dict[str, Any], as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(human)


def _round_context(args: argparse.Namespace) -> tuple[artifacts.Layout, int]:
    """Resolve --outputs/--round for the commands that operate on a round.

    Raises rather than emitting: ``main`` already turns a ValueError into the
    same message with EXIT_USAGE.
    """
    layout = artifacts.layout_for(args.outputs)
    number = args.round or layout.latest_round()
    if number is None:
        raise ValueError("no rounds found — run new-round first")
    return layout, number


# ---------------------------------------------------------------- new-round

def cmd_new_round(args: argparse.Namespace) -> int:
    layout = artifacts.layout_for(args.outputs)
    number = (layout.latest_round() or 0) + 1
    directory = layout.round_dir(number)
    directory.mkdir(parents=True, exist_ok=True)

    write_to = [str(layout.reading_path(number, lens)) for lens in args.lens]
    payload = {"round": number, "dir": str(directory), "write_to": write_to}
    lines = [f"Round {number} -> {directory}"]
    lines += [f"  expects  {Path(p).name}" for p in write_to]
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

    result = compare.compare(readings)
    resolved = artifacts.resolved_ids(layout)
    blocker_ids = [b["id"] for b in result.blockers if b.get("id")]
    novelty = compare.novelty(artifacts.load_state(layout), blocker_ids, resolved)

    open_blockers = [
        b for b in result.blockers if b.get("id") not in resolved
    ]

    payload = {
        "round": number,
        "lenses": result.lenses,
        "lens_count": result.lens_count,
        "counts": {
            "open": len(open_blockers),
            "new": len(novelty["new"]),
            "repeat": len(novelty["repeat"]),
            "already_decided": len(novelty["resolved"]),
            "disagreements": len(result.disagreements),
        },
        "novelty": novelty,
        "disagreements": [d.as_dict() for d in result.disagreements],
        "blockers": open_blockers,
        "incomplete_readings": result.incomplete,
        "findings_path": str(layout.findings_path),
    }

    artifacts.write_json(
        layout.state_path,
        compare.record_round(
            artifacts.load_state(layout),
            number=number,
            lenses=result.lenses,
            blocker_ids=blocker_ids,
        ),
    )
    artifacts.write_json(layout.findings_path, payload)

    _emit(payload, args.json, _render_round(payload))
    return EXIT_OK


def _render_round(payload: dict[str, Any]) -> str:
    counts = payload["counts"]
    lines = [
        f"Round {payload['round']} — {payload['lens_count']} lenses: "
        f"{', '.join(payload['lenses'])}",
        f"  open {counts['open']}   new {counts['new']}   "
        f"seen before {counts['repeat']}   already decided {counts['already_decided']}",
        "",
    ]

    if payload["incomplete_readings"]:
        lines.append("Readings that could not be compared:")
        lines += [f"  {item}" for item in payload["incomplete_readings"]]
        lines.append("")

    if payload["blockers"]:
        lines.append("Open blockers:")
        for blocker in payload["blockers"]:
            found = ", ".join(blocker.get("found_by", [])) or "unknown"
            flag = " (new)" if blocker["id"] in payload["novelty"]["new"] else ""
            lines.append(f"  {blocker['id']}{flag} — {blocker.get('title', '')}")
            lines.append(
                f"      raised by {found}"
                + (f"  in {blocker['where']}" if blocker.get("where") else "")
            )
            if blocker.get("question"):
                lines.append(f"      {blocker['question']}")
            for item in blocker.get("disagreements", []):
                lines.append(f"      readings disagree — {item['question']}")
                for lens, answer in item["answers"].items():
                    lines.append(f"          {lens}: {answer}")
        lines.append("")

    if counts["new"]:
        lines.append(
            f"{counts['new']} blocker(s) not seen in any previous round. "
            "Decide these, then judge whether another round is worth it."
        )
    else:
        lines.append(
            "Nothing new this round. Whether that means the spec is ready is "
            "your call — say so explicitly rather than implying it."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- resolve

def cmd_resolve(args: argparse.Namespace) -> int:
    layout = artifacts.layout_for(args.outputs)
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
    layout = artifacts.layout_for(args.outputs)
    state = artifacts.load_state(layout)
    findings = artifacts.load_findings(layout)
    resolutions = artifacts.load_resolutions(layout)

    payload = {
        "rounds_run": len(state.get("rounds", [])),
        "resolved": len(resolutions),
        "resolutions": resolutions,
        "counts": findings.get("counts", {}),
        "blockers": findings.get("blockers", []),
        "disagreements": findings.get("disagreements", []),
    }

    lines = [
        f"Rounds run      {payload['rounds_run']}",
        f"Resolved        {payload['resolved']}",
        f"Open blockers   {payload['counts'].get('open', 0)}",
        f"Disagreements   {payload['counts'].get('disagreements', 0)}",
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

    new_round = leaf("new-round", "allocate the next round directory")
    new_round.add_argument("--lens", nargs="*", default=[], help="lens names this round")
    new_round.set_defaults(func=cmd_new_round)

    round_cmd = leaf("round", "compare this round's readings")
    round_cmd.add_argument("--round", type=int)
    round_cmd.set_defaults(func=cmd_round)

    resolve = leaf("resolve", "record a decision")
    resolve.add_argument("--id", required=True, help="blocker id")
    resolve.add_argument("--choice", required=True, help="the chosen option label")
    resolve.add_argument("--applied-to", nargs="*", help="spec files updated")
    resolve.add_argument(
        "--source", default="user", choices=["user", "assumed"],
        help="whether the user decided or the skill applied a default",
    )
    resolve.set_defaults(func=cmd_resolve)

    leaf("status", "current refinement state").set_defaults(func=cmd_status)
