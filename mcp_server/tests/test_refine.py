from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from services import refine_artifacts as artifacts
from services import refine_compare as compare

def reading(
    lens: str, *, decisions=None, blockers=None, cells=None, matrices=None
) -> dict[str, Any]:
    payload = {
        "lens": lens,
        "spec_root": "specs",
        "decisions": decisions if decisions is not None else [],
        "blockers": blockers if blockers is not None else [],
    }
    if cells is not None:
        payload["cells"] = cells
    if matrices is not None:
        payload["matrices"] = matrices
    return payload

def grid(*ids: str) -> dict[str, Any]:
    return {
        "cells": [
            {
                "id": cell_id,
                "question": f"what happens on {cell_id}?",
                "where": "specs/orders.md#Checkout",
            }
            for cell_id in ids
        ]
    }

def cell(cell_id: str, value: str, *, guessed: bool = False) -> dict[str, Any]:
    return {"id": cell_id, "value": value, "guessed": guessed}

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
    def test_same_answer_is_not_a_disagreement(self):
        readings = [
            reading("a", decisions=[decision("what store backs orders", "postgres")]),
            reading("b", decisions=[decision("what store backs orders", "postgres")]),
        ]
        found, notes = compare.find_disagreements(readings)
        self.assertEqual(found, [])
        self.assertEqual(notes, [])

    def test_different_answers_are_located(self):
        readings = [
            reading("a", decisions=[decision("what store backs orders", "postgres")]),
            reading("b", decisions=[decision("what store backs orders", "dynamodb")]),
        ]
        found, _ = compare.find_disagreements(readings)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].answers, {"a": "postgres", "b": "dynamodb"})
        self.assertEqual(found[0].where, "specs/orders.md#Checkout")

    def test_rewording_the_question_still_collides(self):
        readings = [
            reading("a", decisions=[decision("What store backs the orders?", "postgres")]),
            reading("b", decisions=[decision("what stores back an order", "dynamodb")]),
        ]
        found, _ = compare.find_disagreements(readings)
        self.assertEqual(len(found), 1)

    def test_unrelated_questions_do_not_collide(self):
        readings = [
            reading("a", decisions=[decision("what store backs orders", "postgres")]),
            reading("b", decisions=[decision("how are retries bounded", "3 attempts")]),
        ]
        found, _ = compare.find_disagreements(readings)
        self.assertEqual(found, [])

    def test_reordering_the_words_is_a_different_question(self):
        readings = [
            reading("a", decisions=[
                decision("Does the hold expire before the payment?", "yes")
            ]),
            reading("b", decisions=[
                decision("Does the payment expire before the hold?", "no")
            ]),
        ]
        found, _ = compare.find_disagreements(readings)
        self.assertEqual(found, [])

    def test_one_lens_answering_twice_keeps_the_first_and_says_so(self):
        readings = [
            reading("a", decisions=[
                decision("what store backs orders", "postgres"),
                decision("what store backs orders", "dynamodb"),
            ]),
        ]
        found, notes = compare.find_disagreements(readings)
        self.assertEqual(found, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("postgres", notes[0])
        self.assertIn("dynamodb", notes[0])

    def test_differing_phrasings_are_all_reported(self):
        readings = [
            reading("a", decisions=[decision("What store backs the orders?", "pg")]),
            reading("b", decisions=[decision("what stores back an order", "ddb")]),
        ]
        found, _ = compare.find_disagreements(readings)
        self.assertEqual(
            found[0].as_dict()["phrasings"],
            {"a": "What store backs the orders?", "b": "what stores back an order"},
        )

    def test_identical_phrasings_are_not_repeated_in_the_payload(self):
        readings = [
            reading("a", decisions=[decision("what store backs orders", "pg")]),
            reading("b", decisions=[decision("what store backs orders", "ddb")]),
        ]
        found, _ = compare.find_disagreements(readings)
        self.assertNotIn("phrasings", found[0].as_dict())

    def test_disagreement_nobody_raised_becomes_its_own_blocker(self):
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
        result = compare.compare([
            reading("a",
                    decisions=[decision("what store backs orders", "postgres")],
                    blockers=[blocker("store-choice")]),
            reading("b", decisions=[decision("what store backs orders", "dynamodb")]),
        ])
        self.assertEqual([b["id"] for b in result.blockers], ["store-choice"])
        self.assertEqual(len(result.blockers[0]["disagreements"]), 1)
        self.assertEqual(len(result.disagreements), 1)

    def test_many_disagreements_at_one_location_stay_many_blockers(self):
        result = compare.compare([
            reading("a", blockers=[blocker("expiry")], decisions=[
                decision("how long is the hold", "5m"),
                decision("who cancels an order", "the buyer"),
                decision("what currency is stored", "usd"),
            ]),
            reading("b", decisions=[
                decision("how long is the hold", "15m"),
                decision("who cancels an order", "support"),
                decision("what currency is stored", "eur"),
            ]),
        ])
        self.assertEqual(len(result.disagreements), 3)
        self.assertEqual(len(result.blockers), 3)
        self.assertEqual(len(result.blockers[0]["disagreements"]), 1)

    def test_item_count_is_the_same_with_or_without_a_host_blocker(self):
        decisions_a = [decision("how long is the hold", "5m"),
                       decision("who cancels an order", "the buyer")]
        decisions_b = [decision("how long is the hold", "15m"),
                       decision("who cancels an order", "support")]
        with_host = compare.compare([
            reading("a", blockers=[blocker("expiry")], decisions=decisions_a),
            reading("b", decisions=decisions_b),
        ])
        without_host = compare.compare([
            reading("a", decisions=decisions_a),
            reading("b", decisions=decisions_b),
        ])
        self.assertEqual(len(with_host.blockers), len(without_host.blockers))

    def test_slug_collision_attaches_instead_of_dropping_the_disagreement(self):
        shared = "should the order be cancelled when the"
        result = compare.compare([
            reading("a", decisions=[
                {"question": f"{shared} payment fails", "value": "yes",
                 "where": "specs/a.md"},
                {"question": f"{shared} hold expires", "value": "yes",
                 "where": "specs/b.md"},
            ]),
            reading("b", decisions=[
                {"question": f"{shared} payment fails", "value": "no",
                 "where": "specs/a.md"},
                {"question": f"{shared} hold expires", "value": "no",
                 "where": "specs/b.md"},
            ]),
        ])
        self.assertEqual(len(result.disagreements), 2)
        self.assertEqual(len(result.blockers), 1)
        self.assertEqual(len(result.blockers[0]["disagreements"]), 1)

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

class TestMalformedReadings(unittest.TestCase):
    def test_blockers_as_an_object_is_reported_not_crashed(self):
        result = compare.compare([
            {"lens": "bad", "decisions": [], "blockers": {"expiry": {"id": "x"}}},
            reading("good", blockers=[blocker("expiry")]),
        ])
        self.assertEqual(result.lens_count, 1)
        self.assertEqual([b["id"] for b in result.blockers], ["expiry"])
        self.assertIn("blockers should be a list of objects", result.incomplete[0])

    def test_decisions_as_a_string_is_reported_not_crashed(self):
        result = compare.compare([{"lens": "bad", "decisions": "oops", "blockers": []}])
        self.assertEqual(result.lens_count, 0)
        self.assertIn("decisions should be a list of objects", result.incomplete[0])

    def test_cells_as_a_string_is_reported_not_crashed(self):
        result = compare.compare(
            [{"lens": "bad", "decisions": [], "blockers": [], "cells": "oops"}],
            grid=grid("hold.timeout"),
        )
        self.assertEqual(result.lens_count, 0)
        self.assertIn("cells should be a list of objects", result.incomplete[0])

    def test_a_list_with_a_non_object_entry_is_still_rejected(self):
        result = compare.compare([
            {"lens": "bad", "decisions": [{"question": "q", "value": "v"}, "oops"],
             "blockers": []},
        ])
        self.assertEqual(result.lens_count, 0)

    def test_uncomparable_readings_are_not_counted_as_lenses(self):
        result = compare.compare([
            reading("a"),
            {"lens": "bad", "decisions": "oops", "blockers": []},
        ])
        self.assertEqual(result.lens_count, 1)
        self.assertEqual(result.readings_total, 2)
        self.assertEqual(result.lenses, ["a"])

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

    def test_the_filename_wins_over_a_lens_field_that_disagrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(
                layout.reading_path(1, "ordering"),
                reading("concurrency", decisions=[decision("what store", "pg")]),
            )
            artifacts.write_json(
                layout.reading_path(1, "concurrency"),
                reading("concurrency", decisions=[decision("what store", "ddb")]),
            )
            loaded = artifacts.load_readings(layout, 1)
            self.assertEqual(
                sorted(r["lens"] for r in loaded), ["concurrency", "ordering"]
            )

            result = compare.compare(loaded)
            self.assertEqual(len(result.disagreements), 1)
            self.assertEqual(len(result.notes), 1)
            self.assertIn("fan-out", result.notes[0])

    def test_a_matching_lens_field_raises_no_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.reading_path(1, "ordering"), reading("ordering"))
            result = compare.compare(artifacts.load_readings(layout, 1))
            self.assertEqual(result.notes, [])

    def test_a_grid_with_the_wrong_shape_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.grid_path(1), {"cells": "oops"})
            with self.assertRaises(ValueError) as ctx:
                artifacts.load_grid(layout, 1)
            self.assertIn(artifacts.GRID_FILE, str(ctx.exception))

    def test_a_coherence_file_with_the_wrong_shape_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.coherence_path(1), {"blockers": {"a": {}}})
            with self.assertRaises(ValueError) as ctx:
                artifacts.load_coherence(layout, 1)
            self.assertIn(artifacts.COHERENCE_FILE, str(ctx.exception))

    def test_hand_edited_resolutions_of_the_wrong_shape_name_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            artifacts.write_json(layout.resolutions_path, {"resolved": "expiry"})
            with self.assertRaises(ValueError) as ctx:
                artifacts.load_resolutions(layout)
            self.assertIn(artifacts.RESOLUTIONS_FILE, str(ctx.exception))

    def test_everything_lives_under_the_loop_s_own_subdirectory(self):
        layout = artifacts.layout_for("docs")
        for path in (
            layout.resolutions_path,
            layout.findings_path,
            layout.grid_path(1),
            layout.reading_path(1, "ordering"),
        ):
            self.assertIn(artifacts.REFINE_SUBDIR, path.parts)
            self.assertNotIn("analysis", path.parts)
            self.assertNotIn("planning", path.parts)

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

    def test_a_fresh_project_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(tmp)
            self.assertEqual(layout.rounds(), [])
            self.assertEqual(artifacts.load_resolutions(layout), [])
            self.assertEqual(artifacts.load_findings(layout), {})

class TestGridCoverage(unittest.TestCase):
    def test_cell_no_lens_answered_is_reported(self):
        coverage, _ = compare.grid_coverage(
            grid("hold.timeout", "hold.cancel"),
            [reading("a", cells=[cell("hold.timeout", "seat released")])],
        )
        self.assertEqual(coverage.cells_total, 2)
        self.assertEqual(coverage.cells_filled, 1)
        self.assertEqual([c["id"] for c in coverage.uncovered], ["hold.cancel"])

    def test_two_lenses_filling_a_cell_differently_is_a_disagreement(self):
        _, disagreements = compare.grid_coverage(
            grid("hold.timeout"),
            [
                reading("a", cells=[cell("hold.timeout", "seat released")]),
                reading("b", cells=[cell("hold.timeout", "hold extended")]),
            ],
        )
        self.assertEqual(len(disagreements), 1)
        self.assertEqual(
            disagreements[0].answers, {"a": "seat released", "b": "hold extended"}
        )

    def test_agreement_reached_by_guessing_is_reported_not_silent(self):
        coverage, disagreements = compare.grid_coverage(
            grid("hold.timeout"),
            [
                reading("a", cells=[cell("hold.timeout", "released", guessed=True)]),
                reading("b", cells=[cell("hold.timeout", "released", guessed=True)]),
            ],
        )
        self.assertEqual(disagreements, [])
        self.assertEqual(coverage.agreed_guesses[0]["lenses"], ["a", "b"])

    def test_agreement_one_lens_read_from_the_spec_is_not_flagged(self):
        coverage, _ = compare.grid_coverage(
            grid("hold.timeout"),
            [
                reading("a", cells=[cell("hold.timeout", "released", guessed=True)]),
                reading("b", cells=[cell("hold.timeout", "released")]),
            ],
        )
        self.assertEqual(coverage.agreed_guesses, [])

    def test_cell_conflict_becomes_a_blocker_through_the_normal_path(self):
        result = compare.compare(
            [
                reading("a", cells=[cell("hold.timeout", "released")]),
                reading("b", cells=[cell("hold.timeout", "extended")]),
            ],
            grid=grid("hold.timeout"),
        )
        self.assertTrue(any(b.get("from_disagreement") for b in result.blockers))

    def test_cell_conflict_attaches_to_a_blocker_at_the_same_place(self):
        result = compare.compare(
            [
                reading("a", blockers=[blocker("expiry")],
                        cells=[cell("hold.timeout", "released")]),
                reading("b", cells=[cell("hold.timeout", "extended")]),
            ],
            grid=grid("hold.timeout"),
        )
        self.assertEqual([b["id"] for b in result.blockers], ["expiry"])
        self.assertEqual(len(result.blockers[0]["disagreements"]), 1)

    def test_a_round_without_a_grid_still_compares(self):
        result = compare.compare([reading("a"), reading("b")])
        self.assertIsNone(result.coverage)

def matrix(name="ops × collisions", rows=("hold", "seat"), cols=("claim", "cancel"),
           cells=()) -> dict[str, Any]:
    return {"name": name, "rows": list(rows), "cols": list(cols), "cells": list(cells)}

class TestMatrixCoverage(unittest.TestCase):
    def test_every_intersection_is_counted_whether_or_not_it_was_filled(self):
        report = compare.matrix_coverage([
            reading("a", matrices=[matrix(cells=[
                {"row": "hold", "col": "claim", "value": "409"},
            ])]),
        ])
        self.assertEqual(report[0].declared, 4)
        self.assertEqual(report[0].answered, 1)
        self.assertEqual(len(report[0].missing), 3)

    def test_a_cell_the_lens_could_not_answer_is_a_finding_with_a_reason(self):
        report = compare.matrix_coverage([
            reading("a", matrices=[matrix(cells=[
                {"row": "hold", "col": "claim", "unanswerable": "nobody owns the timer"},
            ])]),
        ])
        self.assertEqual(report[0].answered, 0)
        self.assertEqual(
            report[0].unanswerable,
            [{"row": "hold", "col": "claim", "why": "nobody owns the timer"}],
        )
        self.assertEqual(len(report[0].missing), 3)

    def test_guessed_answers_are_counted_separately_from_answered(self):
        report = compare.matrix_coverage([
            reading("a", matrices=[matrix(cells=[
                {"row": "hold", "col": "claim", "value": "409", "guessed": True},
                {"row": "hold", "col": "cancel", "value": "released"},
            ])]),
        ])
        self.assertEqual(report[0].answered, 2)
        self.assertEqual(report[0].guessed, 1)

    def test_matrices_are_never_merged_across_lenses(self):
        report = compare.matrix_coverage([
            reading("a", matrices=[matrix(name="same title")]),
            reading("b", matrices=[matrix(name="same title")]),
        ])
        self.assertEqual([m.lens for m in report], ["a", "b"])

    def test_the_least_complete_reading_is_reported_first(self):
        report = compare.matrix_coverage([
            reading("thorough", matrices=[matrix(name="x", cells=[
                {"row": r, "col": c, "value": "v"}
                for r in ("hold", "seat") for c in ("claim", "cancel")
            ])]),
            reading("thin", matrices=[matrix(name="y")]),
        ])
        self.assertEqual([m.lens for m in report], ["thin", "thorough"])

    def test_a_matrix_with_no_axes_is_not_a_cross_product(self):
        report = compare.matrix_coverage([
            reading("a", matrices=[matrix(rows=(), cols=())]),
        ])
        self.assertEqual(report, [])

    def test_axes_of_the_wrong_shape_are_reported_not_counted(self):
        result = compare.compare([
            {"lens": "a", "decisions": [], "blockers": [],
             "matrices": [{"name": "x", "rows": "hold", "cols": ["claim"], "cells": []}]},
        ])
        self.assertEqual(result.lens_count, 0)
        self.assertIn("matrices[0].rows", result.incomplete[0])

    def test_a_round_without_matrices_still_compares(self):
        result = compare.compare([reading("a"), reading("b")])
        self.assertEqual(result.matrices, [])

    def test_nothing_about_a_matrix_blocks_or_fails_the_round(self):
        result = compare.compare([
            reading("a", matrices=[matrix()]),
        ])
        self.assertEqual(result.blockers, [])
        self.assertEqual(len(result.matrices[0].missing), 4)

class TestCoherence(unittest.TestCase):
    def test_coherence_blockers_reach_the_user_attributed(self):
        result = compare.compare(
            [reading("a"), reading("b")],
            coherence={"blockers": [blocker("locking-contradicts-retry")]},
        )
        found = {b["id"]: b["found_by"] for b in result.blockers}
        self.assertEqual(found["locking-contradicts-retry"], ["coherence"])

    def test_coherence_does_not_count_as_an_independent_reading(self):
        result = compare.compare(
            [reading("a"), reading("b")],
            coherence={"blockers": [blocker("x")]},
        )
        self.assertEqual(result.lens_count, 2)
        self.assertEqual(result.lenses, ["a", "b"])

    def test_coherence_never_votes_in_a_disagreement(self):
        result = compare.compare(
            [reading("a", decisions=[decision("what store", "postgres")])],
            coherence={
                "blockers": [],
                "decisions": [decision("what store", "mysql")],
            },
        )
        self.assertEqual(result.disagreements, [])

class TestEndToEnd(unittest.TestCase):
    def test_resolved_blocker_drops_out_of_the_next_round(self):
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
