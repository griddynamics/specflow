"""Usage counting of the LLM gateway (docs/graph/api.md "LLM gateway", row "Usage").

Counts are kept per call class and prompt name: logical calls, failed calls, HTTP requests, input and
output tokens, and wall-clock duration of the calls (backoff included).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from graph.core.config import CallClass


class UsageCounts(BaseModel):
    """Counts of one call class and prompt name, or a sum over several."""

    model_config = ConfigDict(frozen=True)

    calls: int = 0
    failures: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0


class UsageEntry(UsageCounts):
    """Counts of one call class and prompt name."""

    call_class: CallClass
    prompt: str


class UsageSnapshot(BaseModel):
    """Usage at one moment; ``entries`` sorted by call class, then prompt name."""

    model_config = ConfigDict(frozen=True)

    entries: tuple[UsageEntry, ...] = ()

    def total(self, *, call_class: CallClass | None = None, prompt: str | None = None) -> UsageCounts:
        """Sum of the entries matching the given call class and prompt name (None matches any)."""

        selected = [
            entry
            for entry in self.entries
            if (call_class is None or entry.call_class == call_class) and (prompt is None or entry.prompt == prompt)
        ]
        return UsageCounts(
            calls=sum(entry.calls for entry in selected),
            failures=sum(entry.failures for entry in selected),
            requests=sum(entry.requests for entry in selected),
            input_tokens=sum(entry.input_tokens for entry in selected),
            output_tokens=sum(entry.output_tokens for entry in selected),
            duration_s=sum(entry.duration_s for entry in selected),
        )


@dataclass
class _Counter:
    calls: int = 0
    failures: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0


class UsageRecorder:
    """Mutable counters behind ``Gateway.usage()``; used from one event loop."""

    def __init__(self) -> None:
        self._counters: dict[tuple[CallClass, str], _Counter] = {}

    def _counter(self, call_class: CallClass, prompt: str) -> _Counter:
        return self._counters.setdefault((call_class, prompt), _Counter())

    def request(self, call_class: CallClass, prompt: str) -> None:
        self._counter(call_class, prompt).requests += 1

    def tokens(self, call_class: CallClass, prompt: str, input_tokens: int, output_tokens: int) -> None:
        counter = self._counter(call_class, prompt)
        counter.input_tokens += input_tokens
        counter.output_tokens += output_tokens

    def call(self, call_class: CallClass, prompt: str, *, duration_s: float, failed: bool) -> None:
        counter = self._counter(call_class, prompt)
        counter.calls += 1
        counter.failures += int(failed)
        counter.duration_s += duration_s

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(
            entries=tuple(
                UsageEntry(
                    call_class=call_class,
                    prompt=prompt,
                    calls=counter.calls,
                    failures=counter.failures,
                    requests=counter.requests,
                    input_tokens=counter.input_tokens,
                    output_tokens=counter.output_tokens,
                    duration_s=counter.duration_s,
                )
                for (call_class, prompt), counter in sorted(self._counters.items())
            )
        )
