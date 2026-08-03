#!/usr/bin/env python3
"""Single entry point for every SpecFlow oracle.

One dispatcher rather than seven scripts, for a practical reason: a skill has to
name a path to invoke anything, and that path is the one fragile thing in a
plugin. Keeping it to one file per skill invocation minimises the surface, and
Python resolves its own siblings from __file__ regardless of how it was called.

Commands the refinement loop actually uses:

    new-round   allocate the next round directory
    round       validate, merge, rank, and decide whether to stop  <- the workhorse
    resolve     record a decision so later rounds stop asking
    status      render current state
    contracts   check emitted SQL/API against the model
    mutate      inject a known defect and verify it gets caught (internal)

Exit codes: 0 success, 1 checks failed, 2 bad usage. The non-zero on failure is
the point — a skill cannot quietly proceed past a gate that did not pass.
"""

from __future__ import annotations

import argparse
import json
import sys
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


def _validate_round(layout: artifacts.Layout, number: int) -> dict[str, Any]:
    loaded = artifacts.load_interpretations(layout, number)
    if not loaded:
        return {"round": number, "lenses": [], "ok": False, "error": "no interpretation files found"}
    reports = [_validate_one(item) for item in loaded]
    return {
        "round": number,
        "lenses": reports,
        "ok": all(r["ok"] for r in reports),
    }


def cmd_validate(args: argparse.Namespace) -> int:
    layout = artifacts.layout_for(args.outputs)
    number = args.round or layout.latest_round()
    if number is None:
        _emit({"error": "no rounds found"}, args.json, "No rounds found. Run new-round first.")
        return EXIT_USAGE

    result = _validate_round(layout, number)
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
    layout = artifacts.layout_for(args.outputs)
    number = args.round or layout.latest_round()
    if number is None:
        _emit({"error": "no rounds found"}, args.json, "No rounds found. Run new-round first.")
        return EXIT_USAGE

    validation = _validate_round(layout, number)
    if not validation["ok"]:
        _emit(
            {"stage": "validate", "validation": validation},
            args.json,
            _render_validation(validation),
        )
        return EXIT_FAILED

    interpretations = artifacts.load_interpretations(layout, number)
    merged = concordance.compute(interpretations)

    model_issues: list[str] = []
    for interpretation in interpretations:
        report = contracts.check_model(interpretation)
        model_issues += [f"{interpretation.get('lens')}: {issue}" for issue in map(str, report.issues)]

    resolved = artifacts.resolved_ids(layout)
    ranked = rank.rank(merged.blockers, lens_count=merged.lens_count, already_resolved=resolved)
    buckets = rank.partition(ranked)
    summary = rank.summarize(ranked)

    state = artifacts.load_state(layout)
    verdict = saturation.evaluate(
        state,
        ranked,
        round_number=number,
        lens_count=merged.lens_count,
        resolved=resolved,
        required_streak=args.consecutive,
    )

    artifacts.write_json(layout.state_path, saturation.updated_state(state, verdict))
    artifacts.write_json(
        layout.blockers_path,
        {
            "round": number,
            "lens_count": merged.lens_count,
            "summary": summary,
            "ask": buckets[rank.ASK],
            "assume": buckets[rank.ASSUME],
            "note": buckets[rank.NOTE],
            "divergences": [d.as_dict() for d in merged.divergences],
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
        "divergences": [d.as_dict() for d in merged.divergences],
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
    blockers: dict[str, Any] = {}
    if layout.blockers_path.exists():
        blockers = artifacts.read_json(layout.blockers_path)

    payload = {
        "rounds_run": len(state.get("rounds", [])),
        "converged": state.get("converged", False),
        "dry_streak": state.get("dry_streak", 0),
        "resolved": len(resolutions),
        "open_ask": len(blockers.get("ask", [])),
        "assumed": len(blockers.get("assume", [])),
        "noted": len(blockers.get("note", [])),
        "resolutions": resolutions,
        "ask": blockers.get("ask", []),
    }

    lines = [
        f"Rounds run       {payload['rounds_run']}",
        f"Converged        {'yes' if payload['converged'] else 'no'}",
        f"Resolved         {payload['resolved']}",
        f"Open decisions   {payload['open_ask']}",
        f"Assumed          {payload['assumed']}",
        f"Noted            {payload['noted']}",
    ]
    if payload["ask"]:
        lines.append("")
        lines.append("Still open:")
        lines += [f"  {b['id']} — {b['title']}" for b in payload["ask"]]
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


# ---------------------------------------------------------------- contracts

def cmd_contracts(args: argparse.Namespace) -> int:
    layout = artifacts.layout_for(args.outputs)
    number = args.round or layout.latest_round()
    if number is None:
        _emit({"error": "no rounds found"}, args.json, "No rounds found.")
        return EXIT_USAGE

    interpretations = artifacts.load_interpretations(layout, number)
    if not interpretations:
        _emit({"error": "no interpretations"}, args.json, f"No lens artifacts in round {number}.")
        return EXIT_USAGE

    sql = Path(args.sql).read_text(encoding="utf-8") if args.sql else None
    api = Path(args.api).read_text(encoding="utf-8") if args.api else None

    findings = []
    for interpretation in interpretations:
        report = contracts.check_model(interpretation)
        if sql or api:
            emitted = contracts.check_emitted(interpretation, sql=sql, api=api)
            report.issues.extend(emitted.issues)
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

def cmd_mutate(args: argparse.Namespace) -> int:
    if args.mutate_command == "apply":
        manifest = mutate.apply_mutation(
            Path(args.spec_dir),
            Path(args.into),
            kind=args.kind,
            index=args.index,
        )
        out = Path(args.into) / "mutation-manifest.json"
        artifacts.write_json(out, manifest.as_dict())
        first = manifest.mutations[0]
        _emit(
            {"manifest": manifest.as_dict(), "manifest_path": str(out)},
            args.json,
            f"Applied {first.kind} to {first.file}:{first.line}\n"
            f"  was: {first.original}\n"
            f"  now: {first.replacement or '<deleted>'}\n"
            f"Manifest: {out}",
        )
        return EXIT_OK

    manifest = artifacts.read_json(Path(args.manifest))
    layout = artifacts.layout_for(args.outputs)
    blockers_doc = artifacts.read_json(layout.blockers_path) if layout.blockers_path.exists() else {}
    all_blockers = (
        blockers_doc.get("ask", []) + blockers_doc.get("assume", []) + blockers_doc.get("note", [])
    )
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
    contracts_cmd.add_argument("--sql", help="emitted DDL file")
    contracts_cmd.add_argument("--api", help="emitted API contract (JSON)")
    contracts_cmd.set_defaults(func=cmd_contracts)

    mutate_cmd = subparsers.add_parser("mutate", help="inject a defect and verify detection")
    mutate_subs = mutate_cmd.add_subparsers(dest="mutate_command", required=True)

    apply_cmd = mutate_subs.add_parser("apply")
    apply_cmd.add_argument("--spec-dir", required=True)
    apply_cmd.add_argument("--into", required=True, help="destination for the mutated copy")
    apply_cmd.add_argument("--kind", required=True, choices=list(mutate.MUTATIONS))
    apply_cmd.add_argument("--index", type=int, default=0, help="which eligible line (deterministic)")
    apply_cmd.set_defaults(func=cmd_mutate)

    verify_cmd = with_outputs(mutate_subs.add_parser("verify"))
    verify_cmd.add_argument("--manifest", required=True)
    verify_cmd.set_defaults(func=cmd_mutate)

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
