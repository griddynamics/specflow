"""Tests for the refinement comparison and bookkeeping.

Scope note: these test comparison and memory, because that is all the code does.
There is deliberately no test that a reading is "complete" or that a spec is
"ready" — those are judgments the skill makes, and a test asserting them would
just be pinning down an arbitrary threshold.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from services import refine_artifacts as artifacts
from services import refine_compare as compare


def reading(lens: str, *, decisions=None, blockers=None) -> dict[str, Any]:
    return {
        "lens": lens,
        "spec_root": "specs",
        "decisions": decisions if decisions is not None else [],
        "blockers": blockers if blockers is not None else [],
    }


def decision(question: str, value: str, *, guessed: bool = False) -> dict[str, Any]:
    return {
        "question": question,
        "value": value,
        "where": "specs/orders.md#Checkout",
        "guessed": guessed,
    }


def blocker(identifier: str, *, title: str = "", options: int = 2) -> dict[str, Any]:
    return {
        "id": identifier,
        "title": title or f"decision {identifier}",
        "question": f"which way for {identifier}?",
        "where": "specs/orders.md#Checkout",
        "options": [
            {"label": f"opt{i}", "consequence": "..."} for i in range(options)
        ],
        "recommended": "opt0",
        "impact": "changes_behaviour",
        "reversible": True,
    }


class TestDisagreement(unittest.TestCase):
    """The signal: independent readings landing on different answers."""

    def test_same_answer_is_not_a_disagreement(self):
        readings = [
            reading("a", decisions=[decision("what store backs orders", "postgres")]),
            reading("b", decisions=[decision("what store backs orders", "postgres")]),
        ]
        self.assertEqual(compare.find_disagreements(readings), [])

    def test_different_answers_are_located(self):
        readings = [
            reading("a", decisions=[decision("what store backs orders", "postgres")]),
            reading("b", decisions=[decision("what store backs orders", "dynamodb")]),
        ]
        found = compare.find_disagreements(readings)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].answers, {"a": "postgres", "b": "dynamodb"})
        self.assertEqual(found[0].where, "specs/orders.md#Checkout")

    def test_rewording_the_question_still_collides(self):
        """Two lenses will not phrase the same question identically."""
        readings = [
            reading("a", decisions=[decision("What store backs the orders?", "postgres")]),
            reading("b", decisions=[decision("what stores back an order", "dynamodb")]),
        ]
        self.assertEqual(len(compare.find_disagreements(readings)), 1)

    def test_unrelated_questions_do_not_collide(self):
        readings = [
            reading("a", decisions=[decision("what store backs orders", "postgres")]),
            reading("b", decisions=[decision("how are retries bounded", "3 attempts")]),
        ]
        self.assertEqual(compare.find_disagreements(readings), [])

    def test_disagreement_nobody_raised_becomes_its_own_blocker(self):
        """The valuable case: a gap no single reading noticed."""
        result = compare.compare([
            reading("a", decisions=[decision("what store backs orders", "postgres")]),
            reading("b", decisions=[decision("what store backs orders", "dynamodb")]),
        ])
        derived = [b for b in result.blockers if b.get("from_disagreement")]
        self.assertEqual(len(derived), 1)
        self.assertEqual(
            {o["label"] for o in derived[0]["options"]}, {"postgres", "dynamodb"}
        )

    def test_disagreement_attaches_to_a_blocker_at_the_same_place(self):
        """One gap must not appear as two list items."""
        result = compare.compare([
            reading("a",
                    decisions=[decision("what store backs orders", "postgres")],
                    blockers=[blocker("store-choice")]),
            reading("b", decisions=[decision("what store backs orders", "dynamodb")]),
        ])
        self.assertEqual([b["id"] for b in result.blockers], ["store-choice"])
        self.assertEqual(len(result.blockers[0]["disagreements"]), 1)
        # Still reported as a disagreement in its own right.
        self.assertEqual(len(result.disagreements), 1)

    def test_synthesized_id_is_readable_and_stable(self):
        result = compare.compare([
            reading("a", decisions=[decision("which datastore backs bookings", "pg")]),
            reading("b", decisions=[decision("which datastore backs bookings", "ddb")]),
        ])
        derived = [b for b in result.blockers if b.get("from_disagreement")][0]
        self.assertEqual(derived["id"], "diverged-which-datastore-backs-bookings")

    def test_three_way_disagreement_ranks_above_two_way(self):
        result = compare.compare([
            reading("a", decisions=[decision("q one alpha", "x"), decision("q two beta", "p")]),
            reading("b", decisions=[decision("q one alpha", "y"), decision("q two beta", "q")]),
            reading("c", decisions=[decision("q one alpha", "z"), decision("q two beta", "p")]),
        ])
        self.assertEqual(result.disagreements[0].as_dict()["distinct"], 3)


class TestMergeBlockers(unittest.TestCase):
    def test_same_id_from_three_lenses_merges_with_attribution(self):
        merged = compare.merge_blockers([
            reading("a", blockers=[blocker("expiry")]),
            reading("b", blockers=[blocker("expiry")]),
            reading("c", blockers=[blocker("expiry")]),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["found_by"], ["a", "b", "c"])

    def test_richest_option_set_wins(self):
        merged = compare.merge_blockers([
            reading("a", blockers=[blocker("expiry", options=2)]),
            reading("b", blockers=[blocker("expiry", options=4)]),
        ])
        self.assertEqual(len(merged[0]["options"]), 4)

    def test_widely_raised_blockers_sort_first(self):
        merged = compare.merge_blockers([
            reading("a", blockers=[blocker("lonely"), blocker("popular")]),
            reading("b", blockers=[blocker("popular")]),
            reading("c", blockers=[blocker("popular")]),
        ])
        self.assertEqual(merged[0]["id"], "popular")

    def test_blocker_without_id_is_skipped(self):
        merged = compare.merge_blockers([reading("a", blockers=[{"title": "no id"}])])
        self.assertEqual(merged, [])

    def test_reading_missing_keys_is_reported_not_crashed(self):
        result = compare.compare([{"lens": "broken"}])
        self.assertEqual(len(result.incomplete), 1)
        self.assertIn("decisions", result.incomplete[0])


class TestNovelty(unittest.TestCase):
    """Bookkeeping across rounds — what code is genuinely better at."""

    def test_first_round_is_all_new(self):
        result = compare.novelty({"rounds": []}, ["a", "b"], set())
        self.assertEqual(result["new"], ["a", "b"])
        self.assertEqual(result["repeat"], [])

    def test_previously_seen_is_a_repeat_not_new(self):
        state = compare.record_round(
            {"rounds": []}, number=1, lenses=["x"], blocker_ids=["a"]
        )
        result = compare.novelty(state, ["a", "b"], set())
        self.assertEqual(result["new"], ["b"])
        self.assertEqual(result["repeat"], ["a"])

    def test_resolved_never_counts_as_new(self):
        result = compare.novelty({"rounds": []}, ["a", "b"], {"a"})
        self.assertEqual(result["new"], ["b"])
        self.assertEqual(result["resolved"], ["a"])

    def test_rerunning_a_round_replaces_its_record(self):
        state = compare.record_round(
            {"rounds": []}, number=1, lenses=["x"], blocker_ids=["a"]
        )
        state = compare.record_round(
            state, number=1, lenses=["x", "y"], blocker_ids=["a", "b"]
        )
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(state["rounds"][0]["blocker_ids"], ["a", "b"])


class TestLayout(unittest.TestCase):
    def test_round_allocation_and_reading_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            self.assertIsNone(layout.latest_round())

            path = layout.reading_path(1, "concurrency")
            artifacts.write_json(path, reading("concurrency"))
            self.assertEqual(layout.latest_round(), 1)

            loaded = artifacts.load_readings(layout, 1)
            self.assertEqual([r["lens"] for r in loaded], ["concurrency"])

    def test_lens_name_is_derived_from_the_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            path = layout.reading_path(1, "ordering")
            artifacts.write_json(path, {"decisions": [], "blockers": []})
            self.assertEqual(artifacts.load_readings(layout, 1)[0]["lens"], "ordering")

    def test_broken_json_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "x.json"
            bad.write_text("{not json")
            with self.assertRaises(ValueError) as ctx:
                artifacts.read_json(bad)
            self.assertIn("x.json", str(ctx.exception))

    def test_resolutions_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(
                layout.resolutions_path,
                {"resolved": [{"blocker_id": "expiry", "choice": "refund"}]},
            )
            self.assertEqual(artifacts.resolved_ids(layout), {"expiry"})

    def test_missing_state_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            self.assertEqual(artifacts.load_state(layout), {"rounds": []})
            self.assertEqual(artifacts.load_resolutions(layout), [])
            self.assertEqual(artifacts.load_findings(layout), {})


class TestEndToEnd(unittest.TestCase):
    def test_resolved_blocker_drops_out_of_the_next_round(self):
        """The loop must not re-ask a decision the user already made."""
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            for lens in ("a", "b"):
                artifacts.write_json(
                    layout.reading_path(1, lens),
                    reading(lens, blockers=[blocker("expiry")]),
                )
            first = compare.compare(artifacts.load_readings(layout, 1))
            self.assertIn("expiry", [b["id"] for b in first.blockers])

            artifacts.write_json(
                layout.resolutions_path,
                {"resolved": [{"blocker_id": "expiry", "choice": "refund"}]},
            )
            resolved = artifacts.resolved_ids(layout)
            still_open = [b for b in first.blockers if b["id"] not in resolved]
            self.assertEqual(still_open, [])

    def test_findings_file_is_valid_json_for_the_reporting_skill(self):
        result = compare.compare([
            reading("a", decisions=[decision("what store", "postgres")],
                    blockers=[blocker("expiry")]),
            reading("b", decisions=[decision("what store", "mysql")]),
        ])
        payload = {
            "disagreements": [d.as_dict() for d in result.disagreements],
            "blockers": result.blockers,
        }
        self.assertEqual(json.loads(json.dumps(payload))["blockers"][0]["id"], "expiry")


if __name__ == "__main__":
    unittest.main(verbosity=2)
