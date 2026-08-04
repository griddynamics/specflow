"""Regression tests for the SpecFlow oracles.

Runs under the MCP server suite (``make unit-tests``), so a change to a schema,
a totality rule or a ranking threshold cannot land green without these passing.

Every test here corresponds to a defect the loop must keep catching. If one
starts failing, the loop has become less able to find real specification gaps —
which is the only thing this product does.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from services.oracles import artifacts, concordance, contracts, mutate, rank, saturation, totality
from services.oracles.jsonschema_mini import validate_as


def anchor(
    file: str = "specs/orders.md", section: str = "Checkout", inferred: bool = False
) -> dict[str, Any]:
    value: dict[str, Any] = {"file": file, "section": section}
    if inferred:
        value["inferred"] = True
    return value


def valid_interpretation(lens: str = "concurrency") -> dict[str, Any]:
    """A minimal artifact that passes schema and totality. The baseline."""
    return {
        "lens": lens,
        "spec_root": "specs",
        "dimensions": {
            "part_a": {
                "persistence": {"value": "relational", "spec_anchor": anchor()},
                "infrastructure_complexity": {"value": "single_process", "spec_anchor": anchor()},
                "scale_target": {"value": "small_team", "spec_anchor": anchor()},
                "technology_stack": {"value": "Python 3.12 / FastAPI / Postgres 16", "spec_anchor": anchor()},
                "quality_level": {"value": "production", "spec_anchor": anchor()},
                "scope_boundaries": {
                    "in_scope": ["checkout"],
                    "out_of_scope": ["refunds"],
                    "spec_anchor": anchor(),
                },
            },
            "part_d": {
                "naming": {
                    "files": "snake_case",
                    "identifiers": "snake_case",
                    "database": "plural snake_case",
                    "api_paths": "kebab-case",
                },
                "patterns": {
                    "error_handling": "exceptions at boundary",
                    "validation_boundary": "request schema",
                    "async_style": "async/await",
                    "config_source": "env vars",
                },
            },
        },
        "entities": [
            {
                "name": "Order",
                "identity": "id",
                "spec_anchor": anchor(),
                "fields": [
                    {"name": "id", "type": "uuid", "required": True},
                    {"name": "total", "type": "decimal", "required": False, "derived": True},
                ],
            }
        ],
        "operations": [
            {
                "name": "createOrder",
                "kind": "create",
                "entity": "Order",
                "idempotent": False,
                "authorization": "authenticated buyer",
                "spec_anchor": anchor(),
            }
        ],
        "state_machines": [
            {
                "entity": "Order",
                "states": ["pending", "paid"],
                "events": ["pay", "expire"],
                "spec_anchor": anchor(),
                "matrix": [
                    {"state": "pending", "event": "pay", "outcome": "paid"},
                    {"state": "pending", "event": "expire", "outcome": "reject"},
                    {"state": "paid", "event": "pay", "outcome": "reject"},
                    {"state": "paid", "event": "expire", "outcome": "reject"},
                ],
            }
        ],
        "failure_modes": [],
        "phases": [{"number": 1, "name": "Checkout core", "delivers": ["createOrder"]}],
        "blockers": [
            {
                "id": "paid-order-expiry",
                "title": "What happens when a paid order's reservation expires",
                "spec_anchor": anchor(),
                "scenario": "Payment settles after the hold lapses.",
                "question": "Honour the order or refund it?",
                "options": [
                    {"label": "honour", "consequence": "may oversell"},
                    {"label": "refund", "consequence": "buyer loses the item"},
                ],
                "recommended": "refund",
                "impact": "changes_behaviour",
                "reversible": False,
            }
        ],
        "assumptions": [],
    }


class TestBaseline(unittest.TestCase):
    """The fixture itself must be clean, or every other test is meaningless."""

    def test_schema_valid(self):
        result = validate_as(valid_interpretation(), "specflow/interpretation")
        self.assertTrue(result.ok, [str(p) for p in result.problems])

    def test_totality_clean(self):
        report = totality.check(valid_interpretation())
        self.assertTrue(report.ok, [str(f) for f in report.findings])


class TestTotalityGate(unittest.TestCase):
    """The forcing function. Each of these is a way to skip real work."""

    def _findings(self, mutate_fn) -> list[str]:
        artifact = valid_interpretation()
        mutate_fn(artifact)
        return [str(f) for f in totality.check(artifact).findings]

    def test_rejects_partial_state_matrix(self):
        def drop_rows(a):
            a["state_machines"][0]["matrix"] = a["state_machines"][0]["matrix"][:2]

        findings = self._findings(drop_rows)
        self.assertTrue(any("matrix is partial" in f for f in findings), findings)

    def test_rejects_evasion_value(self):
        def evade(a):
            a["dimensions"]["part_d"]["patterns"]["config_source"] = "TBD"

        findings = self._findings(evade)
        self.assertTrue(any("evasion" in f for f in findings), findings)

    def test_rejects_unresolvable_operation_entity(self):
        def dangle(a):
            a["operations"].append({"name": "archiveInvoice", "kind": "command", "entity": "Invoice"})

        findings = self._findings(dangle)
        self.assertTrue(any("unknown entity 'Invoice'" in f for f in findings), findings)

    def test_rejects_unresolvable_foreign_key(self):
        def dangle(a):
            a["entities"][0]["fields"].append(
                {"name": "customer_id", "type": "uuid", "required": True, "references": "Customer"}
            )

        findings = self._findings(dangle)
        self.assertTrue(any("unknown entity 'Customer'" in f for f in findings), findings)

    def test_rejects_inferred_value_with_no_blocker(self):
        """The loophole that matters most: admitting a gap without raising it."""

        def infer_silently(a):
            a["dimensions"]["part_a"]["scale_target"]["spec_anchor"] = anchor(
                file="specs/unrelated.md", section="", inferred=True
            )

        findings = self._findings(infer_silently)
        self.assertTrue(any("marked inferred" in f for f in findings), findings)

    def test_rejects_undefined_matrix_outcome_with_no_blocker(self):
        def undefined_without_blocker(a):
            a["state_machines"][0]["matrix"][3]["outcome"] = "undefined_in_spec"
            a["state_machines"][0]["spec_anchor"] = anchor(file="specs/unrelated.md", section="")

        findings = self._findings(undefined_without_blocker)
        self.assertTrue(any("undefined in the spec" in f for f in findings), findings)

    def test_rejects_recommendation_outside_options(self):
        def bad_recommendation(a):
            a["blockers"][0]["recommended"] = "something-else"

        findings = self._findings(bad_recommendation)
        self.assertTrue(any("not one of the options" in f for f in findings), findings)

    def test_accepts_admitted_gap_when_raised(self):
        """The escape hatch is legitimate — as long as the gap is surfaced."""
        artifact = valid_interpretation()
        artifact["state_machines"][0]["matrix"][3]["outcome"] = "undefined_in_spec"
        report = totality.check(artifact)
        self.assertTrue(report.ok, [str(f) for f in report.findings])


class TestContracts(unittest.TestCase):
    def test_detects_required_and_derived_contradiction(self):
        artifact = valid_interpretation()
        artifact["entities"][0]["fields"][1]["required"] = True  # total is already derived
        issues = [str(i) for i in contracts.check_model(artifact).issues]
        self.assertTrue(any("contradiction" in i for i in issues), issues)

    def test_detects_circular_required_reference(self):
        artifact = valid_interpretation()
        artifact["entities"][0]["fields"].append(
            {"name": "reservation_id", "type": "uuid", "required": True, "references": "Reservation"}
        )
        artifact["entities"].append({
            "name": "Reservation",
            "identity": "id",
            "fields": [
                {"name": "id", "type": "uuid", "required": True},
                {"name": "order_id", "type": "uuid", "required": True, "references": "Order"},
            ],
        })
        issues = [str(i) for i in contracts.check_model(artifact).issues]
        self.assertTrue(any("circular-requirement" in i for i in issues), issues)

    def test_detects_unguarded_mutation(self):
        artifact = valid_interpretation()
        del artifact["operations"][0]["authorization"]
        issues = [str(i) for i in contracts.check_model(artifact).issues]
        self.assertTrue(any("unguarded-mutation" in i for i in issues), issues)

    def test_detects_missing_table_in_emitted_ddl(self):
        artifact = valid_interpretation()
        report = contracts.check_emitted(artifact, sql="CREATE TABLE unrelated (id uuid PRIMARY KEY);")
        issues = [str(i) for i in report.issues]
        self.assertTrue(any("missing-table" in i for i in issues), issues)

    def test_detects_dangling_api_ref(self):
        artifact = valid_interpretation()
        api = json.dumps({
            "paths": {"/orders": {"post": {"responses": {"200": {"$ref": "#/components/schemas/Ghost"}}}}},
            "components": {"schemas": {}},
        })
        issues = [str(i) for i in contracts.check_emitted(artifact, api=api).issues]
        self.assertTrue(any("dangling-ref" in i for i in issues), issues)


class TestConcordance(unittest.TestCase):
    def test_dimension_disagreement_becomes_a_blocker(self):
        first = valid_interpretation("concurrency")
        second = valid_interpretation("ordering")
        second["dimensions"]["part_a"]["persistence"]["value"] = "event_sourced"

        result = concordance.compute([first, second])
        divergences = [d.where for d in result.divergences]
        self.assertIn("part_a.persistence", divergences)
        self.assertTrue(any(b["id"].startswith("divergent-") for b in result.blockers))

    def test_same_blocker_from_two_lenses_merges(self):
        first = valid_interpretation("concurrency")
        second = valid_interpretation("ordering")
        result = concordance.compute([first, second])
        merged = [b for b in result.blockers if b["id"] == "paid-order-expiry"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(sorted(merged[0]["found_by"]), ["concurrency", "ordering"])

    def test_agreement_produces_no_divergence(self):
        result = concordance.compute([valid_interpretation("a"), valid_interpretation("b")])
        self.assertEqual([d for d in result.divergences if d.kind == "dimension"], [])


class TestRanking(unittest.TestCase):
    def _blocker(self, **overrides):
        base = {
            "id": "b1",
            "title": "t",
            "options": [{"label": "x", "consequence": "c"}, {"label": "y", "consequence": "c"}],
            "recommended": "x",
            "impact": "changes_behaviour",
            "reversible": True,
            "found_by": ["one"],
        }
        base.update(overrides)
        return base

    def test_blocking_impact_is_always_asked(self):
        ranked = rank.rank([self._blocker(impact="blocks_build")], lens_count=6)
        self.assertEqual(ranked[0].disposition, rank.ASK)

    def test_reversible_low_impact_is_assumed(self):
        ranked = rank.rank([self._blocker(reversible=True)], lens_count=6)
        self.assertEqual(ranked[0].disposition, rank.ASSUME)

    def test_irreversible_is_asked(self):
        ranked = rank.rank([self._blocker(reversible=False)], lens_count=6)
        self.assertEqual(ranked[0].disposition, rank.ASK)

    def test_lone_cosmetic_finding_is_only_noted(self):
        ranked = rank.rank([self._blocker(impact="cosmetic", found_by=["one"])], lens_count=6)
        self.assertEqual(ranked[0].disposition, rank.NOTE)

    def test_resolved_blockers_are_dropped(self):
        ranked = rank.rank([self._blocker()], lens_count=6, already_resolved={"b1"})
        self.assertEqual(ranked, [])

    def test_concordance_raises_rank(self):
        many = self._blocker(id="many", found_by=["a", "b", "c", "d", "e", "f"])
        few = self._blocker(id="few", found_by=["a"])
        ranked = rank.rank([few, many], lens_count=6)
        self.assertEqual(ranked[0].blocker["id"], "many")


class TestSaturation(unittest.TestCase):
    def test_new_blockers_prevent_convergence(self):
        ranked = rank.rank(
            [{"id": "new-one", "impact": "blocks_build", "reversible": False, "found_by": ["a"]}],
            lens_count=1,
        )
        verdict = saturation.evaluate({}, ranked, round_number=1, lens_count=1)
        self.assertFalse(verdict.converged)

    def test_dry_round_converges(self):
        verdict = saturation.evaluate({}, [], round_number=1, lens_count=1)
        self.assertTrue(verdict.converged)

    def test_resolved_blocker_counts_as_seen(self):
        ranked = rank.rank(
            [{"id": "known", "impact": "blocks_build", "reversible": False, "found_by": ["a"]}],
            lens_count=1,
        )
        verdict = saturation.evaluate(
            {}, ranked, round_number=2, lens_count=1, resolved={"known"}
        )
        self.assertTrue(verdict.converged)

    def test_two_consecutive_required(self):
        first = saturation.evaluate({}, [], round_number=1, lens_count=1, required_streak=2)
        self.assertFalse(first.converged)
        state = saturation.updated_state({}, first)
        second = saturation.evaluate(state, [], round_number=2, lens_count=1, required_streak=2)
        self.assertTrue(second.converged)


class TestMutationHarness(unittest.TestCase):
    SPEC = (
        "# Orders\n\n## Checkout\n"
        "A buyer must hold a reservation before an order is created here.\n"
        "An order shall never be created without a matching reservation record.\n"
        "The reservation expires after 15 minutes if payment has not settled yet.\n"
    )

    def test_drop_constraint_records_what_it_damaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = root / "specs"
            specs.mkdir()
            (specs / "orders.md").write_text(self.SPEC)

            manifest = mutate.apply_mutation(specs, root / "mutated", kind="drop_constraint", index=0)
            self.assertEqual(len(manifest.mutations), 1)
            damaged = manifest.mutations[0]
            self.assertEqual(damaged.file, "orders.md")
            self.assertTrue(damaged.original)
            self.assertNotIn(damaged.original, (root / "mutated" / "orders.md").read_text())

    def test_contradict_inverts_a_modal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = root / "specs"
            specs.mkdir()
            (specs / "orders.md").write_text(self.SPEC)

            manifest = mutate.apply_mutation(specs, root / "mutated", kind="contradict", index=0)
            damaged = manifest.mutations[0]
            self.assertNotEqual(damaged.original, damaged.replacement)
            self.assertTrue(damaged.replacement)

    def test_verify_requires_localization_not_just_detection(self):
        manifest = {"mutations": [{"kind": "drop_constraint", "expect_anchor_file": "orders.md"}]}

        elsewhere = [{"id": "x", "spec_anchor": {"file": "specs/other.md"}}]
        self.assertFalse(mutate.verify(manifest, elsewhere)["passed"])

        on_target = [{"id": "y", "spec_anchor": {"file": "specs/orders.md"}}]
        self.assertTrue(mutate.verify(manifest, on_target)["passed"])

    def test_deterministic_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = root / "specs"
            specs.mkdir()
            (specs / "orders.md").write_text(self.SPEC)
            first = mutate.apply_mutation(specs, root / "a", kind="drop_constraint", index=1)
            second = mutate.apply_mutation(specs, root / "b", kind="drop_constraint", index=1)
            self.assertEqual(first.mutations[0].original, second.mutations[0].original)


class TestArtifactLayout(unittest.TestCase):
    def test_round_allocation_and_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(Path(tmp) / "docs")
            self.assertIsNone(layout.latest_round())

            path = layout.interpretation_path(1, "concurrency")
            artifacts.write_json(path, valid_interpretation())
            self.assertEqual(layout.latest_round(), 1)

            loaded = artifacts.load_interpretations(layout, 1)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["lens"], "concurrency")

    def test_resolutions_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = artifacts.layout_for(Path(tmp) / "docs")
            artifacts.write_json(
                layout.resolutions_path, {"resolved": [{"blocker_id": "b1", "choice": "x"}]}
            )
            self.assertEqual(artifacts.resolved_ids(layout), {"b1"})


class TestJsonSchemaValidator(unittest.TestCase):
    def test_unimplemented_keyword_raises_rather_than_passing(self):
        """A silently-ignored constraint is worse than no constraint."""
        from services.oracles.jsonschema_mini import validate

        with self.assertRaises(ValueError):
            validate({}, {"type": "object", "propertyNames": {"type": "string"}})

    def test_enum_and_pattern_enforced(self):
        artifact = valid_interpretation()
        artifact["dimensions"]["part_a"]["persistence"]["value"] = "carrier_pigeon"
        self.assertFalse(validate_as(artifact, "specflow/interpretation").ok)

    def test_additional_properties_rejected(self):
        artifact = valid_interpretation()
        artifact["unexpected_key"] = True
        self.assertFalse(validate_as(artifact, "specflow/interpretation").ok)
