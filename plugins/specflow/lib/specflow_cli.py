#!/usr/bin/env python3
"""Single entry point for every SpecFlow oracle.

One dispatcher rather than seven scripts, for a practical reason: a skill has to
name a path to invoke anything, and that path is the one fragile thing in a
plugin. Keeping it to one file per skill invocation minimises the surface, and
Python resolves its own siblings from __file__ regardless of how it was called.

Commands the refinement loop actually uses:

    new-round         allocate the next round directory
    round             validate, merge, rank, decide whether to stop  <- the workhorse
    resolve           record a decision so later rounds stop asking
    status            render current state
    contracts         check emitted SQL/API against the model
    check-dimensions  gate the analysis artifact before the loop starts
    mutate            inject a known defect and verify it gets caught (internal)

Exit codes: 0 success, 1 checks failed, 2 bad usage. The non-zero on failure is
the point — a skill cannot quietly proceed past a gate that did not pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Skills invoke this by absolute path from any working directory, so make the
# sibling package importable before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from specflow import (  # noqa: E402  (must follow the sys.path bootstrap)
    artifacts,
    concordance,
    contracts,
    mutate,
    rank,
    saturation,
    totality,
)
from specflow.jsonschema_mini import validate_as  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _emit(payload: dict[str, Any], as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(human)


def _round_context(args: argparse.Namespace) -> tuple[artifacts.Layout, int]:
    """Resolve --outputs/--round for the commands that operate on a round.

    Raises rather than emitting: ``main`` already turns a ValueError into the
    same JSON-or-stderr message with EXIT_USAGE, so three copies of the guard
    (which had already drifted into two different wordings) collapse to one.
    """
    layout = artifacts.layout_for(args.outputs)
    number = args.round or layout.latest_round()
    if number is None:
        raise ValueError("no rounds found — run new-round first")
    return layout, number


# ---------------------------------------------------------------- new-round

def cmd_new_round(args: argparse.Namespace) -> int:
    layout = artifacts.layout_for(args.outputs)
    latest = layout.latest_round() or 0
    number = latest + 1
    directory = layout.round_dir(number)
    directory.mkdir(parents=True, exist_ok=True)

    payload = {
        "round": number,
        "dir": str(directory),
        "write_to": [
            str(layout.interpretation_path(number, lens)) for lens in args.lens
        ],
    }
    lines = [f"Round {number} -> {directory}"]
    lines += [f"  expects  {Path(p).name}" for p in payload["write_to"]]
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


# ---------------------------------------------------------------- validate

def _validate_one(interpretation: dict[str, Any]) -> dict[str, Any]:
    """Schema conformance then totality, for one lens artifact."""
    payload = {k: v for k, v in interpretation.items() if not k.startswith("_")}
    schema_result = validate_as(payload, "specflow/interpretation")
    problems = [str(p) for p in schema_result.problems]

    # Totality only means something on a structurally valid artifact.
    gaps: list[str] = []
    if schema_result.ok:
        gaps = [str(f) for f in totality.check(interpretation).findings]

    return {
        "lens": interpretation.get("lens"),
        "path": interpretation.get("_path"),
        "schema_problems": problems,
        "totality_gaps": gaps,
        "ok": not problems and not gaps,
    }


def _validate_round(number: int, loaded: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate already-loaded artifacts — the caller owns the (single) read."""
    if not loaded:
        return {"round": number, "lenses": [], "ok": False, "error": "no interpretation files found"}
    reports = [_validate_one(item) for item in loaded]
    return {
        "round": number,
        "lenses": reports,
        "ok": all(r["ok"] for r in reports),
    }


def cmd_validate(args: argparse.Namespace) -> int:
    layout, number = _round_context(args)
    result = _validate_round(number, artifacts.load_interpretations(layout, number))
    _emit(result, args.json, _render_validation(result))
    return EXIT_OK if result["ok"] else EXIT_FAILED


def _render_validation(result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"Round {result['round']}: {result['error']}"
    lines = [f"Round {result['round']} — {len(result['lenses'])} lens artifact(s)"]
    for report in result["lenses"]:
        mark = "ok" if report["ok"] else "FAIL"
        lines.append(f"  [{mark}] {report['lens']}")
        for problem in report["schema_problems"]:
            lines.append(f"        schema: {problem}")
        for gap in report["totality_gaps"]:
            lines.append(f"        totality: {gap}")
    if not result["ok"]:
        lines.append("")
        lines.append("Artifacts are not total. Fix them and re-run — do not proceed.")
    return "\n".join(lines)


# ---------------------------------------------------------------- round

def cmd_round(args: argparse.Namespace) -> int:
    """Validate, merge, rank, and decide whether to stop. One call per round."""
    layout, number = _round_context(args)

    interpretations = artifacts.load_interpretations(layout, number)
    validation = _validate_round(number, interpretations)
    if not validation["ok"]:
        _emit(
            {"stage": "validate", "validation": validation},
            args.json,
            _render_validation(validation),
        )
        return EXIT_FAILED

    merged = concordance.compute(interpretations)

    model_issues: list[str] = []
    for interpretation in interpretations:
        report = contracts.check_model(interpretation)
        model_issues += [f"{interpretation.get('lens')}: {issue}" for issue in map(str, report.issues)]

    resolved = artifacts.resolved_ids(layout)
    ranked = rank.rank(merged.blockers, lens_count=merged.lens_count, already_resolved=resolved)
    buckets = rank.partition(ranked)
    summary = rank.summarize(buckets)

    state = artifacts.load_state(layout)
    verdict = saturation.evaluate(
        state,
        ranked,
        round_number=number,
        lens_count=merged.lens_count,
        resolved=resolved,
        required_streak=args.consecutive,
    )

    divergences = [asdict(d) for d in merged.divergences]
    by_disposition = {d.value: buckets[d] for d in rank.Disposition}

    artifacts.write_json(layout.state_path, saturation.updated_state(state, verdict))
    artifacts.write_json(
        layout.blockers_path,
        {
            "round": number,
            "lens_count": merged.lens_count,
            "summary": summary,
            **by_disposition,
            "divergences": divergences,
            "contract_issues": model_issues,
        },
    )

    payload = {
        "stage": "complete",
        "round": number,
        "lens_count": merged.lens_count,
        "summary": summary,
        "converged": verdict.converged,
        "saturation": verdict.as_dict(),
        "ask": buckets[rank.ASK],
        "assume": buckets[rank.ASSUME],
        "divergences": divergences,
        "contract_issues": model_issues,
        "blockers_path": str(layout.blockers_path),
    }
    _emit(payload, args.json, _render_round(payload))
    return EXIT_OK


def _render_round(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        f"Round {payload['round']} — {payload['lens_count']} lenses",
        f"  ask {summary['ask']}   assume {summary['assume']}   note {summary['note']}",
        "",
    ]

    if payload["divergences"]:
        lines.append("Located disagreements:")
        for divergence in payload["divergences"]:
            lines.append(f"  {divergence['where']}: {divergence['detail']}")
            for lens, value in divergence["lenses"].items():
                lines.append(f"      {lens}: {value}")
        lines.append("")

    if payload["contract_issues"]:
        lines.append("Contract issues:")
        lines += [f"  {issue}" for issue in payload["contract_issues"]]
        lines.append("")

    if payload["ask"]:
        lines.append("Needs a decision:")
        for blocker in payload["ask"]:
            found = ", ".join(blocker.get("found_by", []))
            lines.append(f"  [{blocker['_score']}] {blocker['id']} — {blocker['title']}")
            lines.append(f"        raised by: {found or 'unknown'}  ({blocker['_rationale']})")
            lines.append(f"        {blocker.get('question', '')}")
        lines.append("")

    if payload["assume"]:
        lines.append("Assuming (recorded, reversible):")
        for blocker in payload["assume"]:
            lines.append(f"  {blocker['id']} -> {blocker.get('recommended')}")
        lines.append("")

    lines.append(
        "CONVERGED — " + payload["saturation"]["reason"]
        if payload["converged"]
        else "NOT CONVERGED — " + payload["saturation"]["reason"]
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
    resolutions = artifacts.load_resolutions(layout)
    blockers = artifacts.load_blockers(layout)
    counts = {d.value: len(blockers.get(d.value, [])) for d in rank.Disposition}

    payload = {
        "rounds_run": len(state.get("rounds", [])),
        "converged": state.get("converged", False),
        "dry_streak": state.get("dry_streak", 0),
        "resolved": len(resolutions),
        "counts": counts,
        "resolutions": resolutions,
        "ask": blockers.get(rank.ASK.value, []),
        "assume": blockers.get(rank.ASSUME.value, []),
        # Carried through so a reporting skill does not have to re-read
        # blockers.json behind the CLI — and hardcode its path to do it.
        "divergences": blockers.get("divergences", []),
        "contract_issues": blockers.get("contract_issues", []),
    }

    lines = [
        f"Rounds run       {payload['rounds_run']}",
        f"Converged        {'yes' if payload['converged'] else 'no'}",
        f"Resolved         {payload['resolved']}",
        f"Open decisions   {counts[rank.ASK.value]}",
        f"Assumed          {counts[rank.ASSUME.value]}",
        f"Noted            {counts[rank.NOTE.value]}",
    ]
    if payload["ask"]:
        lines.append("")
        lines.append("Still open:")
        lines += [f"  {b['id']} — {b['title']}" for b in payload["ask"]]
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


# ---------------------------------------------------------------- contracts

def _read_emitted(layout: artifacts.Layout, args: argparse.Namespace) -> contracts.Emitted:
    """Read and parse the emitted artifacts once, for every lens in the round.

    Paths default to the layout, so the skill that emits them and the check that
    reads them cannot disagree about where they live.
    """
    def read(override: str | None, default: Path) -> str | None:
        # An explicit path that does not exist is an error worth reporting; the
        # layout default is simply absent until the emit step has run.
        if override:
            return Path(override).read_text(encoding="utf-8")
        return default.read_text(encoding="utf-8") if default.is_file() else None

    return contracts.Emitted.parse(
        sql=read(args.sql, layout.schema_sql_path),
        api=read(args.api, layout.api_contract_path),
    )


def cmd_contracts(args: argparse.Namespace) -> int:
    layout, number = _round_context(args)

    interpretations = artifacts.load_interpretations(layout, number)
    if not interpretations:
        _emit({"error": "no interpretations"}, args.json, f"No lens artifacts in round {number}.")
        return EXIT_USAGE

    emitted = _read_emitted(layout, args)

    findings = []
    for interpretation in interpretations:
        report = contracts.check_model(interpretation)
        if not emitted.empty:
            report.issues.extend(emitted.check(interpretation).issues)
        findings.append({
            "lens": interpretation.get("lens"),
            "issues": [str(issue) for issue in report.issues],
            "ok": report.ok,
        })

    ok = all(f["ok"] for f in findings)
    lines = []
    for finding in findings:
        lines.append(f"[{'ok' if finding['ok'] else 'FAIL'}] {finding['lens']}")
        lines += [f"      {issue}" for issue in finding["issues"]]
    _emit({"round": number, "findings": findings, "ok": ok}, args.json, "\n".join(lines) or "No issues.")
    return EXIT_OK if ok else EXIT_FAILED


# ---------------------------------------------------------------- mutate

def cmd_mutate_apply(args: argparse.Namespace) -> int:
    manifest = mutate.apply_mutation(
        Path(args.spec_dir),
        Path(args.into),
        kind=args.kind,
        index=args.index,
    )
    out = Path(args.into) / "mutation-manifest.json"
    payload = asdict(manifest)
    artifacts.write_json(out, payload)
    first = manifest.mutations[0]
    _emit(
        {"manifest": payload, "manifest_path": str(out)},
        args.json,
        f"Applied {first.kind} to {first.file}:{first.line}\n"
        f"  was: {first.original}\n"
        f"  now: {first.replacement or '<deleted>'}\n"
        f"Manifest: {out}",
    )
    return EXIT_OK


def cmd_mutate_verify(args: argparse.Namespace) -> int:
    manifest = artifacts.read_json(Path(args.manifest))
    blockers_doc = artifacts.load_blockers(artifacts.layout_for(args.outputs))
    all_blockers = [
        blocker
        for disposition in rank.Disposition
        for blocker in blockers_doc.get(disposition.value, [])
    ]
    result = mutate.verify(manifest, all_blockers)

    lines = [f"Mutations: {result['mutations']}   localized: {result['localized']}"]
    for entry in result["results"]:
        mark = "PASS" if entry["localized"] else "MISS"
        lines.append(f"  [{mark}] {entry['kind']} expected in {entry['expected_file']}")
        if entry["matching_blockers"]:
            lines.append(f"          matched: {', '.join(entry['matching_blockers'])}")
    lines.append("")
    lines.append("PASS — the loop detects and localizes injected ambiguity." if result["passed"]
                 else "FAIL — an injected defect was not localized. This is a real bug.")
    _emit(result, args.json, "\n".join(lines))
    return EXIT_OK if result["passed"] else EXIT_FAILED


# ---------------------------------------------------------- check-dimensions

def cmd_check_dimensions(args: argparse.Namespace) -> int:
    """Gate the analysis artifact with the same rules the loop applies later.

    Schema conformance plus the evasion walk. The schema alone accepts
    ``{"value": "TBD"}`` — and an evasion is exactly what the analysis skill
    promises is "rejected mechanically", so the promise has to be a check.
    """
    layout = artifacts.layout_for(args.outputs)
    path = layout.dimensions_path
    document = artifacts.read_json(path)

    schema_result = validate_as(document, "specflow/dimensions")
    problems = [str(p) for p in schema_result.problems]
    gaps = (
        [str(f) for f in totality.evasion_findings(document)]
        if schema_result.ok and isinstance(document, dict)
        else []
    )

    ok = not problems and not gaps
    lines = [f"{path} — {'ok' if ok else 'FAIL'}"]
    lines += [f"  schema: {problem}" for problem in problems]
    lines += [f"  evasion: {gap}" for gap in gaps]
    if not ok:
        lines.append("")
        lines.append("Fix these before describing the analysis as complete.")

    _emit(
        {"path": str(path), "schema_problems": problems, "evasions": gaps, "ok": ok},
        args.json,
        "\n".join(lines),
    )
    return EXIT_OK if ok else EXIT_FAILED


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="specflow",
        description="Deterministic oracles for the SpecFlow refinement loop.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def with_outputs(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--outputs", default="docs", help="outputs dir (default: docs)")
        return sub

    new_round = with_outputs(subparsers.add_parser("new-round", help="allocate the next round"))
    new_round.add_argument("--lens", nargs="*", default=[], help="lens names expected this round")
    new_round.set_defaults(func=cmd_new_round)

    validate = with_outputs(subparsers.add_parser("validate", help="schema + totality only"))
    validate.add_argument("--round", type=int)
    validate.set_defaults(func=cmd_validate)

    round_cmd = with_outputs(subparsers.add_parser("round", help="validate, merge, rank, decide"))
    round_cmd.add_argument("--round", type=int)
    round_cmd.add_argument(
        "--consecutive", type=int, default=1,
        help="consecutive dry rounds required to converge (default 1)",
    )
    round_cmd.set_defaults(func=cmd_round)

    resolve = with_outputs(subparsers.add_parser("resolve", help="record a decision"))
    resolve.add_argument("--id", required=True, help="blocker id")
    resolve.add_argument("--choice", required=True, help="the chosen option label")
    resolve.add_argument("--applied-to", nargs="*", help="spec files updated")
    resolve.add_argument(
        "--source", default="user", choices=["user", "assumed"],
        help="whether the user decided or the default was applied",
    )
    resolve.set_defaults(func=cmd_resolve)

    status = with_outputs(subparsers.add_parser("status", help="current refinement state"))
    status.set_defaults(func=cmd_status)

    contracts_cmd = with_outputs(subparsers.add_parser("contracts", help="check the model and emitted artifacts"))
    contracts_cmd.add_argument("--round", type=int)
    contracts_cmd.add_argument("--sql", help="emitted DDL file (default: the layout's schema.sql)")
    contracts_cmd.add_argument("--api", help="emitted API contract JSON (default: the layout's api.json)")
    contracts_cmd.set_defaults(func=cmd_contracts)

    check_dimensions = with_outputs(
        subparsers.add_parser("check-dimensions", help="schema + evasion check on the analysis artifact")
    )
    check_dimensions.set_defaults(func=cmd_check_dimensions)

    mutate_cmd = subparsers.add_parser("mutate", help="inject a defect and verify detection")
    mutate_subs = mutate_cmd.add_subparsers(dest="mutate_command", required=True)

    apply_cmd = mutate_subs.add_parser("apply")
    apply_cmd.add_argument("--spec-dir", required=True)
    apply_cmd.add_argument("--into", required=True, help="destination for the mutated copy")
    apply_cmd.add_argument("--kind", required=True, choices=list(mutate.MUTATIONS))
    apply_cmd.add_argument("--index", type=int, default=0, help="which eligible line (deterministic)")
    apply_cmd.set_defaults(func=cmd_mutate_apply)

    verify_cmd = with_outputs(mutate_subs.add_parser("verify"))
    verify_cmd.add_argument("--manifest", required=True)
    verify_cmd.set_defaults(func=cmd_mutate_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError, KeyError) as exc:
        message = str(exc).strip("'")
        if args.json:
            print(json.dumps({"error": message}, indent=2))
        else:
            print(f"error: {message}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
