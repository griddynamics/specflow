"""Unit tests for ModelTokenUsage (aggregate + per-model usage)."""
from app.schemas.model_token_usage import FLAT_AGGREGATE_MODEL_NAME, ModelTokenUsage
from app.schemas.workflow_usage_metrics import (
    WORKFLOW_USAGE_METRICS_FIELD,
    aggregate_model_usage_by_workspace,
    model_names_by_workspace,
)


class TestModelTokenUsageAddition:
    def test_adds_all_fields_same_model_name(self):
        a = ModelTokenUsage(
            model_name="m",
            num_turns=2,
            input_tokens=100,
            output_tokens=50,
            cache_write_tokens=10,
            cache_read_tokens=5,
        )
        b = ModelTokenUsage(
            model_name="m",
            num_turns=1,
            input_tokens=40,
            output_tokens=20,
            cache_write_tokens=3,
            cache_read_tokens=2,
        )
        result = a + b
        assert result == ModelTokenUsage(
            model_name="m",
            num_turns=3,
            input_tokens=140,
            output_tokens=70,
            cache_write_tokens=13,
            cache_read_tokens=7,
        )

    def test_add_different_model_names_yields_flat_aggregate_name(self):
        a = ModelTokenUsage(model_name="a", num_turns=1, input_tokens=10)
        b = ModelTokenUsage(model_name="b", num_turns=1, input_tokens=20)
        assert (a + b).model_name == FLAT_AGGREGATE_MODEL_NAME

    def test_add_zero_is_identity(self):
        a = ModelTokenUsage(
            model_name="m",
            num_turns=5,
            input_tokens=300,
            output_tokens=100,
            cache_write_tokens=20,
            cache_read_tokens=10,
        )
        zero = ModelTokenUsage(model_name="m")
        assert a + zero == a


class TestModelTokenUsageTotalTokens:
    def test_sums_all_four_token_fields(self):
        mu = ModelTokenUsage(
            model_name="x",
            num_turns=5,
            input_tokens=1000,
            output_tokens=500,
            cache_write_tokens=200,
            cache_read_tokens=100,
        )
        assert mu.total_tokens == 1800

    def test_total_tokens_zero_when_all_zero(self):
        assert ModelTokenUsage(model_name="x").total_tokens == 0


class TestModelTokenUsageToDict:
    def test_round_trip(self):
        mu = ModelTokenUsage(
            model_name="anthropic/x",
            num_turns=3,
            input_tokens=100,
            output_tokens=50,
            cache_write_tokens=10,
            cache_read_tokens=5,
        )
        assert ModelTokenUsage.from_dict(mu.to_dict()) == mu

    def test_to_dict_includes_model_name(self):
        d = ModelTokenUsage(model_name="m").to_dict()
        assert set(d) == {
            "model_name",
            "num_turns",
            "input_tokens",
            "output_tokens",
            "cache_write_tokens",
            "cache_read_tokens",
        }

    def test_to_flat_dict_omits_model_name(self):
        mu = ModelTokenUsage(
            model_name="m",
            num_turns=1,
            input_tokens=2,
            output_tokens=3,
            cache_write_tokens=4,
            cache_read_tokens=5,
        )
        assert mu.to_flat_dict() == {
            "num_turns": 1,
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_write_tokens": 4,
            "cache_read_tokens": 5,
        }


class TestModelTokenUsageFromGenerationDoc:
    def test_reads_model_usage_nested_dict(self):
        doc = {
            "model_usage": {
                "num_turns": 7,
                "input_tokens": 300,
                "output_tokens": 150,
                "cache_write_tokens": 20,
                "cache_read_tokens": 10,
            }
        }
        mu = ModelTokenUsage.from_generation_session_doc(doc)
        assert mu.model_name == ""
        assert mu.num_turns == 7
        assert mu.input_tokens == 300
        assert mu.output_tokens == 150
        assert mu.cache_write_tokens == 20
        assert mu.cache_read_tokens == 10

    def test_missing_model_usage_returns_zeros(self):
        """Doc without model_usage (e.g. just created) returns zero-valued object."""
        mu = ModelTokenUsage.from_generation_session_doc({})
        assert mu.num_turns == 0
        assert mu.total_tokens == 0

    def test_prefers_flat_model_usage_over_tree(self) -> None:
        """Flat model_usage is read directly (O(1)) when present; tree is the fallback."""
        # In production both fields are always written atomically and stay in sync.
        # from_generation_session_doc prefers the pre-aggregated flat field to avoid O(n) tree scan.
        doc = {
            "model_usage": {
                "num_turns": 7,
                "input_tokens": 300,
                "output_tokens": 150,
                "cache_write_tokens": 20,
                "cache_read_tokens": 10,
            },
            WORKFLOW_USAGE_METRICS_FIELD: {"wf": {"workspaces": {}}},
        }
        mu = ModelTokenUsage.from_generation_session_doc(doc)
        assert mu.model_name == FLAT_AGGREGATE_MODEL_NAME
        assert mu.num_turns == 7
        assert mu.input_tokens == 300

    def test_falls_back_to_workflow_usage_metrics_tree_when_no_flat(self) -> None:
        """Docs that only have workflow_usage_metrics (no model_usage) aggregate the tree."""
        doc = {
            WORKFLOW_USAGE_METRICS_FIELD: {
                "wf": {
                    "workspaces": {
                        "ws": {
                            "models": {
                                "m/a": {
                                    "model_name": "m/a",
                                    "num_turns": 2,
                                    "input_tokens": 10,
                                    "output_tokens": 5,
                                    "cache_write_tokens": 1,
                                    "cache_read_tokens": 1,
                                }
                            }
                        }
                    }
                }
            },
        }
        mu = ModelTokenUsage.from_generation_session_doc(doc)
        assert mu.model_name == FLAT_AGGREGATE_MODEL_NAME
        assert mu.num_turns == 2
        assert mu.input_tokens == 10
        assert mu.output_tokens == 5
        assert mu.cache_write_tokens == 1
        assert mu.cache_read_tokens == 1


class TestModelNamesByWorkspace:
    def test_empty_tree_returns_empty(self) -> None:
        assert model_names_by_workspace(None) == {}
        assert model_names_by_workspace({}) == {}

    def test_collects_distinct_names_per_workspace_across_workflows(self) -> None:
        tree = {
            "phase:1": {
                "workspaces": {
                    "ws-1": {"models": {"m/a": {"model_name": "m/a"}}},
                    "ws-2": {"models": {"m/b": {"model_name": "m/b"}}},
                }
            },
            "phase:2": {
                "workspaces": {
                    # ws-1 used m/a again (dedup) and a new m/c.
                    "ws-1": {"models": {"m/a": {}, "m/c": {}}},
                }
            },
        }
        out = model_names_by_workspace(tree)
        assert out["ws-1"] == ["m/a", "m/c"]
        assert out["ws-2"] == ["m/b"]

    def test_skips_empty_aggregate_model_name(self) -> None:
        tree = {"wf": {"workspaces": {"ws-1": {"models": {FLAT_AGGREGATE_MODEL_NAME: {}}}}}}
        assert model_names_by_workspace(tree) == {"ws-1": []}


class TestWorkflowPrefixScoping:
    """The optional workflow_prefix scopes per-workspace usage/models to a step."""

    def _tree(self) -> dict:
        return {
            "markdown_to_json_converter": {
                "workspaces": {
                    "ws-1": {"models": {"m/haiku": {"model_name": "m/haiku", "num_turns": 1, "input_tokens": 10}}}
                }
            },
            "kb_init": {
                "workspaces": {
                    "ws-1": {"models": {"m/opus": {"model_name": "m/opus", "num_turns": 3, "input_tokens": 100}}}
                }
            },
            "generation_phase_1_coding": {
                "workspaces": {
                    "ws-1": {"models": {"m/sonnet": {"model_name": "m/sonnet", "num_turns": 5, "input_tokens": 200}}}
                }
            },
            "generation_phase_10_coding": {
                "workspaces": {
                    "ws-1": {"models": {"m/other": {"model_name": "m/other", "num_turns": 9, "input_tokens": 9}}}
                }
            },
        }

    def test_plain_key_prefix_selects_only_that_workflow(self) -> None:
        names = model_names_by_workspace(self._tree(), "kb_init")
        assert names == {"ws-1": ["m/opus"]}
        usage = aggregate_model_usage_by_workspace(self._tree(), "kb_init")
        assert usage["ws-1"].num_turns == 3
        assert usage["ws-1"].input_tokens == 100

    def test_phase_prefix_matches_phase_roles_without_bleeding_into_phase_10(self) -> None:
        # Trailing underscore: "generation_phase_1_" must not match "generation_phase_10_coding".
        names = model_names_by_workspace(self._tree(), "generation_phase_1_")
        assert names == {"ws-1": ["m/sonnet"]}

    def test_none_prefix_aggregates_all_workflows(self) -> None:
        names = model_names_by_workspace(self._tree(), None)
        assert set(names["ws-1"]) == {"m/haiku", "m/opus", "m/sonnet", "m/other"}

    def test_no_match_yields_empty(self) -> None:
        assert model_names_by_workspace(self._tree(), "deploy_phase_1_") == {}
        assert aggregate_model_usage_by_workspace(self._tree(), "deploy_phase_1_") == {}
