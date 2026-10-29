from __future__ import annotations

from pathlib import Path

import pytest

from llm.file_formats import (
    CheckpointStore,
    load_markdown_plan,
    parse_markdown_plan,
)


def test_parse_markdown_plan_preserves_order_and_phase_bodies() -> None:
    phases = parse_markdown_plan(
        """# Plan

## Phase 2 — Foundation

Create the package.

## Phase 5 - Feature

Add the feature.
"""
    )

    assert [(phase.number, phase.title) for phase in phases] == [
        (2, "Foundation"),
        (5, "Feature"),
    ]
    assert phases[0].instructions == "Create the package."
    assert phases[1].instructions == "Add the feature."


@pytest.mark.parametrize(
    ("markdown", "message"),
    [
        ("# No phases", "at least one"),
        ("## Phase 1 — Empty\n", "no instructions"),
        (
            "## Phase 2 — Later\nDo it.\n## Phase 1 — Earlier\nDo it.",
            "strictly increasing",
        ),
    ],
)
def test_parse_markdown_plan_rejects_invalid_structure(
    markdown: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_markdown_plan(markdown)


def test_parser_ignores_fenced_and_commented_phase_headings() -> None:
    phases = parse_markdown_plan(
        """<!--
## Phase 0 — Commented
Do not run.
-->
```markdown
## Phase 1 — Example
Do not run.
```
## Phase 2 — Actual
Run this.
"""
    )

    assert [phase.number for phase in phases] == [2]


def test_fence_state_ignores_indented_closer_and_literal_comment() -> None:
    phases = parse_markdown_plan(
        """```markdown
<!-- literal code, not a comment
    ```
## Phase 1 — Still fenced
Do not run.
```
## Phase 2 — Actual
Run this.
"""
    )

    assert [phase.number for phase in phases] == [2]


def test_inline_code_comment_token_does_not_hide_later_phase() -> None:
    phases = parse_markdown_plan(
        """## Phase 1 — Document syntax
Document the literal token `<!--`.

## Phase 2 — Actual
Run this too.
"""
    )

    assert [phase.number for phase in phases] == [1, 2]


def test_load_markdown_plan_reads_from_path(tmp_path: Path) -> None:
    path = tmp_path / "plan.md"
    path.write_text("## Phase 1 — One\nImplement it.")

    phases = load_markdown_plan(path)

    assert phases[0].name == "Phase 1 — One"


def test_checkpoint_tracks_first_incomplete_phase(tmp_path: Path) -> None:
    phases = parse_markdown_plan("## Phase 1 — One\nFirst.\n## Phase 2 — Two\nSecond.\n## Phase 3 — Three\nThird.")
    checkpoint = CheckpointStore(tmp_path / "state.json", phases)

    checkpoint.mark_completed(phases[0])
    checkpoint.mark_completed(phases[1])

    assert checkpoint.completed_phases() == (1, 2)
    assert checkpoint.start_index() == 2


def test_checkpoint_rejects_changed_plan(tmp_path: Path) -> None:
    original = parse_markdown_plan("## Phase 1 — One\nOriginal.")
    changed = parse_markdown_plan("## Phase 1 — One\nChanged.")
    path = tmp_path / "state.json"
    checkpoint = CheckpointStore(path, original)
    checkpoint.mark_completed(original[0])

    with pytest.raises(ValueError, match="does not match"):
        CheckpointStore(path, changed).completed_phases()


def test_checkpoint_rejects_invalid_json_shape(tmp_path: Path) -> None:
    phases = parse_markdown_plan("## Phase 1 — One\nOriginal.")
    checkpoint = CheckpointStore(tmp_path / "state.json", phases)
    checkpoint.path.write_text(
        '{"plan_fingerprint": "' + checkpoint.plan_fingerprint + '", "completed_phases": [true]}'
    )

    with pytest.raises(ValueError, match="invalid checkpoint"):
        checkpoint.completed_phases()
