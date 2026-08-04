from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from services import refine_artifacts as artifacts
from services import refine_commands

def run(*argv: str) -> tuple[int, str]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-path", default=None)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True
    refine_commands.register(subparsers)

    args = parser.parse_args(argv)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = args.func(args)
    return code, buffer.getvalue()

def reading(lens: str, **fields: Any) -> dict[str, Any]:
    return {"lens": lens, "decisions": [], "blockers": [], **fields}

def blocker(identifier: str, *, where: str = "specs/orders.md#Checkout") -> dict[str, Any]:
    return {
        "id": identifier,
        "title": f"decision {identifier}",
        "question": f"which way for {identifier}?",
        "where": where,
        "options": [{"label": "opt0", "consequence": "..."}],
        "recommended": "opt0",
    }

class TestUsageErrors(unittest.TestCase):
    def test_round_with_no_rounds_yet_exits_two_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run("refine", "round", "--outputs", tmp)
            self.assertEqual(code, refine_commands.EXIT_USAGE)
            self.assertIn("new-round", output)

    def test_round_with_no_readings_exits_two_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            layout.round_dir(1).mkdir(parents=True)
            code, output = run("refine", "round", "--outputs", tmp)
            self.assertEqual(code, refine_commands.EXIT_USAGE)
            self.assertIn("reading.", output)

    def test_a_round_of_only_unparseable_readings_exits_two_naming_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            path = layout.reading_path(1, "ordering")
            path.parent.mkdir(parents=True)
            path.write_text("{not json")
            code, output = run("refine", "round", "--outputs", tmp)
            self.assertEqual(code, refine_commands.EXIT_USAGE)
            self.assertIn("reading.ordering.json", output)

    def test_one_unparseable_reading_does_not_cost_the_other_lenses(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(
                layout.reading_path(1, "a"), reading("a", blockers=[blocker("expiry")])
            )
            layout.reading_path(1, "b").write_text('{"lens": "b", "decis')

            code, output = run("refine", "round", "--outputs", tmp)
            self.assertEqual(code, refine_commands.EXIT_OK)
            self.assertIn("1 of 2 readings compared", output)
            self.assertIn("reading.b.json", output)
            self.assertIn("expiry", output)

    def test_a_round_where_nothing_could_be_compared_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(
                layout.reading_path(1, "ordering"), {"lens": "ordering"}
            )
            code, output = run("refine", "round", "--outputs", tmp)
            self.assertEqual(code, refine_commands.EXIT_USAGE)
            self.assertIn("could be compared", output)

    def test_the_error_is_json_when_json_was_asked_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run("refine", "round", "--outputs", tmp, "--json")
            self.assertEqual(code, refine_commands.EXIT_USAGE)
            self.assertIn("error", json.loads(output))

class TestRootPath(unittest.TestCase):
    def test_outputs_is_resolved_against_root_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run("--root-path", tmp, "refine", "new-round", "--outputs", "docs")
            self.assertEqual(code, refine_commands.EXIT_OK)
            self.assertIn(str(Path(tmp) / "docs" / "refine" / "round-01"), output)

    def test_status_reads_the_root_path_tree_not_the_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(Path(tmp) / "docs")
            artifacts.write_json(
                layout.resolutions_path, {"resolved": [{"blocker_id": "expiry"}]}
            )
            code, output = run(
                "--root-path", tmp, "refine", "status", "--outputs", "docs", "--json"
            )
            self.assertEqual(code, refine_commands.EXIT_OK)
            self.assertEqual(json.loads(output)["resolved"], 1)

    def test_an_absolute_outputs_dir_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run(
                "--root-path", "/nonexistent", "refine", "new-round", "--outputs", tmp
            )
            self.assertEqual(code, refine_commands.EXIT_OK)
            self.assertIn(tmp, output)

class TestNoStopRule(unittest.TestCase):
    def test_the_payload_carries_no_novelty_or_stop_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(
                layout.reading_path(1, "a"), reading("a", blockers=[blocker("expiry")])
            )
            payload = json.loads(run("refine", "round", "--outputs", tmp, "--json")[1])
            self.assertNotIn("novelty", payload)
            for absent in ("new", "repeat"):
                self.assertNotIn(absent, payload["counts"])

    def test_no_round_ledger_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.reading_path(1, "a"), reading("a"))
            run("refine", "round", "--outputs", tmp, "--json")
            self.assertEqual(
                sorted(p.name for p in layout.root.iterdir()),
                ["findings.json", "round-01"],
            )

    def test_rounds_are_counted_from_the_directories_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            for number in (1, 2):
                artifacts.write_json(layout.reading_path(number, "a"), reading("a"))
            payload = json.loads(run("refine", "status", "--outputs", tmp, "--json")[1])
            self.assertEqual(payload["rounds_run"], 2)

    def test_rerunning_a_round_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(
                layout.reading_path(1, "a"), reading("a", blockers=[blocker("expiry")])
            )
            first = json.loads(run("refine", "round", "--outputs", tmp, "--json")[1])
            second = json.loads(run("refine", "round", "--outputs", tmp, "--json")[1])
            self.assertEqual(first, second)

class TestPayload(unittest.TestCase):
    def test_round_payload_carries_the_keys_the_skills_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.reading_path(1, "a"), reading("a"))
            payload = json.loads(run("refine", "round", "--outputs", tmp, "--json")[1])
            for key in (
                "round", "lenses", "lens_count", "readings_total", "counts",
                "disagreements", "blockers", "coverage", "matrices",
                "incomplete_readings", "notes", "findings_path",
            ):
                self.assertIn(key, payload)

    def test_a_matrix_cell_the_spec_cannot_answer_reaches_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.reading_path(1, "concurrency"), reading(
                "concurrency",
                matrices=[{
                    "name": "held resource × collision",
                    "rows": ["seat hold"],
                    "cols": ["second claim", "timer expiry"],
                    "cells": [{
                        "row": "seat hold", "col": "second claim",
                        "unanswerable": "nobody owns the timer",
                    }],
                }],
            ))
            code, output = run("refine", "round", "--outputs", tmp)
            self.assertEqual(code, refine_commands.EXIT_OK)
            self.assertIn("0/2 answered", output)
            self.assertIn("spec cannot say", output)
            self.assertIn("nobody owns the timer", output)
            self.assertIn("never filled", output)

            payload = json.loads(run("refine", "round", "--outputs", tmp, "--json")[1])
            self.assertEqual(payload["counts"]["matrix_unanswerable"], 1)
            self.assertEqual(payload["counts"]["matrix_skipped"], 1)

    def test_status_reports_the_matrix_counts_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.reading_path(1, "a"), reading("a", matrices=[{
                "name": "m", "rows": ["r"], "cols": ["c"], "cells": [],
            }]))
            run("refine", "round", "--outputs", tmp, "--json")
            code, output = run("refine", "status", "--outputs", tmp)
            self.assertEqual(code, refine_commands.EXIT_OK)
            self.assertIn("Lens skipped     1 matrix cell(s)", output)

    def test_a_partial_round_says_so_in_the_headline_and_at_the_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.reading_path(1, "a"), reading("a"))
            artifacts.write_json(layout.reading_path(1, "b"), {"lens": "b"})
            code, output = run("refine", "round", "--outputs", tmp)
            self.assertEqual(code, refine_commands.EXIT_OK)
            self.assertIn("1 of 2 readings compared", output)
            self.assertIn("could not be compared", output)

    def test_findings_are_written_for_status_to_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(
                layout.reading_path(1, "a"), reading("a", blockers=[blocker("expiry")])
            )
            run("refine", "round", "--outputs", tmp, "--json")
            findings = artifacts.load_findings(layout)
            self.assertEqual([b["id"] for b in findings["blockers"]], ["expiry"])

    def test_status_drops_a_blocker_resolved_since_the_last_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(
                layout.reading_path(1, "a"),
                reading("a", blockers=[blocker("expiry"), blocker("contention")]),
            )
            run("refine", "round", "--outputs", tmp, "--json")
            run("refine", "resolve", "--outputs", tmp, "--id", "expiry",
                "--choice", "refund")

            payload = json.loads(run("refine", "status", "--outputs", tmp, "--json")[1])
            self.assertEqual(payload["counts"]["open"], 1)
            self.assertEqual([b["id"] for b in payload["blockers"]], ["contention"])

    def test_resolving_twice_is_refused_not_recorded_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ("refine", "resolve", "--outputs", tmp, "--id", "expiry",
                    "--choice", "refund")
            self.assertEqual(run(*args)[0], refine_commands.EXIT_OK)
            code, output = run(*args)
            self.assertEqual(code, refine_commands.EXIT_USAGE)
            self.assertIn("already resolved", output)

if __name__ == "__main__":
    unittest.main(verbosity=2)
