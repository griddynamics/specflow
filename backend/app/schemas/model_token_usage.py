"""LLM usage — token buckets + turns, with optional model identity.

Stored forms:

- Nested ``workflow_usage_metrics`` rows include ``model_name`` (:meth:`to_dict`).
- Top-level Firestore ``model_usage`` (flat cumulative) uses :meth:`to_flat_dict`
  without ``model_name`` for backward compatibility.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Sum across heterogeneous models or legacy flat docs that omit model identity
FLAT_AGGREGATE_MODEL_NAME = ""


@dataclass
class ModelTokenUsage:
    """Cumulative usage for one model, or an aggregate when ``model_name`` is empty."""

    model_name: str
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
        )

    def is_empty(self) -> bool:
        return not (
            self.num_turns
            or self.input_tokens
            or self.output_tokens
            or self.cache_write_tokens
            or self.cache_read_tokens
        )

    def __add__(self, other: ModelTokenUsage) -> ModelTokenUsage:
        name = (
            self.model_name
            if self.model_name == other.model_name
            else FLAT_AGGREGATE_MODEL_NAME
        )
        return ModelTokenUsage(
            model_name=name,
            num_turns=self.num_turns + other.num_turns,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )

    def to_flat_dict(self) -> dict[str, int]:
        """Serialize for Firestore ``model_usage`` / legacy clients (no ``model_name``)."""
        return {
            "num_turns": self.num_turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }

    def to_dict(self) -> dict:
        return asdict(self)

    def merged_with(self, other: ModelTokenUsage) -> ModelTokenUsage:
        """Add numeric fields; ``other.model_name`` must match ``self.model_name``."""
        if other.model_name != self.model_name:
            raise ValueError(
                f"model_name mismatch: {self.model_name!r} vs {other.model_name!r}"
            )
        return ModelTokenUsage(
            model_name=self.model_name,
            num_turns=self.num_turns + other.num_turns,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )

    @classmethod
    def from_dict(cls, data: dict) -> ModelTokenUsage:
        return cls(
            model_name=str(data.get("model_name") or ""),
            num_turns=int(data.get("num_turns") or 0),
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            cache_write_tokens=int(data.get("cache_write_tokens") or 0),
            cache_read_tokens=int(data.get("cache_read_tokens") or 0),
        )

    @classmethod
    def from_generation_session_doc(cls, doc: dict) -> ModelTokenUsage:
        """Read cumulative usage from an generation doc.

        Prefers the flat ``model_usage`` field (always kept in sync by
        ``add_agent_query_token_usage``), falling back to full tree aggregation
        for docs that only have ``workflow_usage_metrics`` (avoids O(n) scan on
        every status poll).
        """
        flat = doc.get("model_usage")
        if flat:
            return cls.from_dict(flat)
        tree = doc.get("workflow_usage_metrics")
        if tree:
            return aggregate_flat_model_usage_from_tree(tree)
        return cls(model_name=FLAT_AGGREGATE_MODEL_NAME)

    @classmethod
    def from_sdk(cls, model_name: str | None, usage: dict, num_turns: int) -> ModelTokenUsage:
        """Map Claude SDK ``usage`` + turn count to ``ModelTokenUsage``."""
        mn = "unknown" if model_name is None else model_name
        return cls(
            model_name=mn,
            num_turns=int(num_turns or 0),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        )

    @classmethod
    def from_sdk_usage(cls, usage: dict, num_turns: int) -> ModelTokenUsage:
        """Delta from a Claude SDK result usage dict without a stable model id (aggregate)."""
        return cls.from_sdk(FLAT_AGGREGATE_MODEL_NAME, usage, num_turns)


def aggregate_flat_model_usage_from_tree(tree: dict) -> ModelTokenUsage:
    """Sum all model rows in a ``workflow_usage_metrics`` tree into one aggregate.

    Shared by :meth:`ModelTokenUsage.from_generation_session_doc` and
    :func:`app.schemas.workflow_usage_metrics.aggregate_flat_model_usage` so that
    neither module needs to import the other (breaking the otherwise circular dependency).
    """
    total = ModelTokenUsage(model_name=FLAT_AGGREGATE_MODEL_NAME)
    for wf_data in tree.values():
        if not isinstance(wf_data, dict):
            continue
        for ws_data in wf_data.get("workspaces", {}).values():
            if not isinstance(ws_data, dict):
                continue
            models = ws_data.get("models") or {}
            if not isinstance(models, dict):
                continue
            for row in models.values():
                if not isinstance(row, dict):
                    continue
                total = total + ModelTokenUsage.from_dict(row)
    return total
