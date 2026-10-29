from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

_PHASE_HEADING = re.compile(
    r"^##\s+Phase\s+(\d+)\s*(?:—|-|:)\s*(.+?)\s*$",
    re.MULTILINE,
)
_FENCE_START = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")


@dataclass(frozen=True, slots=True)
class PlanPhase:
    """One ordered phase parsed from a Markdown plan."""

    number: int
    title: str
    instructions: str

    @property
    def name(self) -> str:
        return f"Phase {self.number} — {self.title}"


class CheckpointStore:
    """Read and atomically update a plan-specific JSON checkpoint."""

    def __init__(self, path: Path, phases: Sequence[PlanPhase]) -> None:
        self.path = path
        self.phases = tuple(phases)
        serialized = json.dumps(
            [
                {
                    "number": phase.number,
                    "title": phase.title,
                    "instructions": phase.instructions,
                }
                for phase in self.phases
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.plan_fingerprint = hashlib.sha256(serialized.encode()).hexdigest()

    def completed_phases(self) -> tuple[int, ...]:
        if not self.path.exists():
            return ()
        data = json.loads(self.path.read_text())
        values = data.get("completed_phases") if isinstance(data, dict) else None
        fingerprint = data.get("plan_fingerprint") if isinstance(data, dict) else None
        if fingerprint != self.plan_fingerprint:
            raise ValueError(f"checkpoint does not match the current plan: {self.path}")
        if not isinstance(values, list) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ValueError(f"invalid checkpoint file: {self.path}")
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate phases in checkpoint file: {self.path}")
        return tuple(values)

    def start_index(self) -> int:
        completed = set(self.completed_phases())
        return next(
            (index for index, phase in enumerate(self.phases) if phase.number not in completed),
            len(self.phases),
        )

    def mark_completed(self, phase: PlanPhase) -> None:
        if phase not in self.phases:
            raise ValueError("cannot checkpoint a phase outside the current plan")
        completed = list(self.completed_phases())
        if phase.number not in completed:
            completed.append(phase.number)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "plan_fingerprint": self.plan_fingerprint,
                    "completed_phases": completed,
                },
                indent=2,
            )
            + "\n"
        )
        temporary.replace(self.path)


def parse_markdown_plan(markdown: str) -> tuple[PlanPhase, ...]:
    """Parse validated, strictly ordered level-two phase sections."""

    visible = mask_markdown_noise(markdown)
    matches = tuple(_PHASE_HEADING.finditer(visible))
    if not matches:
        raise ValueError("plan must contain at least one level-two Phase heading")

    phases: list[PlanPhase] = []
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        instructions = markdown[match.end() : body_end].strip()
        if not instructions:
            raise ValueError(f"Phase {match.group(1)} has no instructions")
        title = match.group(2).strip()
        if not title:
            raise ValueError(f"Phase {match.group(1)} has no title")
        phases.append(
            PlanPhase(
                number=int(match.group(1)),
                title=title,
                instructions=instructions,
            )
        )

    numbers = [phase.number for phase in phases]
    if any(current <= previous for previous, current in pairwise(numbers)):
        raise ValueError("phase numbers must be unique and strictly increasing")
    return tuple(phases)


def load_markdown_plan(path: Path) -> tuple[PlanPhase, ...]:
    """Load and parse a UTF-8 Markdown plan."""

    return parse_markdown_plan(path.read_text())


def mask_markdown_noise(markdown: str) -> str:
    """Blank fenced code, inline code and HTML comments, keeping offsets.

    The result has the same length as the input, so a match position in
    the masked text is a valid position in the original. Callers scanning
    Markdown structure use this so a heading inside a code fence or an
    HTML comment is not mistaken for real structure.
    """

    masker = _NoiseMasker(masked=list(markdown))
    offset = 0
    for line in markdown.splitlines(keepends=True):
        masker.mask_line(line, offset)
        offset += len(line)
    return "".join(masker.masked)


@dataclass(slots=True)
class _NoiseMasker:
    """Line-by-line state of :func:`mask_markdown_noise`."""

    masked: list[str]
    in_fence: tuple[str, int] | None = None
    in_comment: bool = False
    inline_ticks: int | None = None

    def mask_line(self, line: str, offset: int) -> None:
        if self._mask_fence_line(line, offset):
            return
        cursor = 0
        while cursor < len(line):
            cursor = self._scan(line, offset, cursor)

    def _mask_fence_line(self, line: str, offset: int) -> bool:
        marker = _FENCE_START.match(line) if not self.in_comment and self.inline_ticks is None else None
        if self.in_fence is None and marker is not None:
            token = marker.group(1)
            self.in_fence = (token[0], len(token))
            _mask_range(self.masked, offset, offset + len(line))
            return True
        if self.in_fence is None:
            return False
        _mask_range(self.masked, offset, offset + len(line))
        character, minimum = self.in_fence
        closing = re.fullmatch(
            rf"[ ]{{0,3}}({re.escape(character)}{{{minimum},}})[ \t]*",
            line.rstrip("\r\n"),
        )
        if closing is not None:
            self.in_fence = None
        return True

    def _scan(self, line: str, offset: int, cursor: int) -> int:
        """Mask the noise starting at ``cursor`` and return the next cursor."""

        if self.in_comment:
            closing = line.find("-->", cursor)
            end = len(line) if closing < 0 else closing + 3
            _mask_range(self.masked, offset + cursor, offset + end)
            if closing >= 0:
                self.in_comment = False
            return end
        if line[cursor] == "`":
            tick_end = cursor
            while tick_end < len(line) and line[tick_end] == "`":
                tick_end += 1
            tick_count = tick_end - cursor
            if self.inline_ticks is None:
                self.inline_ticks = tick_count
            elif tick_count == self.inline_ticks:
                self.inline_ticks = None
            return cursor + tick_count
        if self.inline_ticks is not None:
            return cursor + 1
        if line.startswith("<!--", cursor):
            _mask_range(self.masked, offset + cursor, offset + cursor + 4)
            self.in_comment = True
            return cursor + 4
        return cursor + 1


def _mask_range(masked: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if masked[index] not in {"\n", "\r"}:
            masked[index] = " "
