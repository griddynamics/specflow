"""Tests for the ``specflow refine`` command group.

These cover the promises the commands make to the *skills that call them*: the
documented exit codes, the payload keys a skill reads, and the fact that a
re-run of the same round is not mistaken for a second round. The comparison
itself is tested in ``test_refine.py``.

Every command is driven through the real parser rather than by calling handlers
directly, because two of the fixes here live in the wiring — the usage-error
wrapper and ``--root-path`` resolution — and a handler-level call would skip
both.
"""

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
    """Parse and dispatch as ``cli.main`` does, capturing stdout."""
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
    """"Exit codes: 0 success, 2 bad usage" is promised in help text and README."""

    def test_round_with_no_rounds_yet_exits_two_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run("refine", "round", "--outputs", tmp)
            self.assertEqual(code, refine_commands.EXIT_USAGE)
            self.assertIn("new-round", output)

    def test_round_with_no_readings_exits_two_with_a_message(self):
        """The likeliest real failure: a lens subagent never wrote its file."""
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
        """Five good readings must not be thrown away over one truncated write."""
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
    """The host CLI's global ``--root-path`` reaches this group too."""

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
    """`round` reports what it compared and passes no verdict on the loop.

    A previous version diffed each round against the previous ones, and the skill
    read "nothing new" as convergence. That inference is unsupported — `new == 0`
    is equally consistent with lenses that found less this round — so the diff,
    its counts, and the ledger behind it are gone. These tests keep them gone.
    """

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
    """Keys the skills read. Adding one is fine; renaming one breaks a skill."""

    def test_round_payload_carries_the_keys_the_skills_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.reading_path(1, "a"), reading("a"))
            payload = json.loads(run("refine", "round", "--outputs", tmp, "--json")[1])
            for key in (
                "round", "lenses", "lens_count", "readings_total", "counts",
                "disagreements", "blockers", "coverage",
                "incomplete_readings", "notes", "findings_path",
            ):
                self.assertIn(key, payload)

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
        """`/specflow-resolve` reads this right after recording one; a stale count
        is how the loop re-asks a decision it was just told to stop asking."""
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
