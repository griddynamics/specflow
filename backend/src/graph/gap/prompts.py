"""The prompts of ``graph.gap`` and their versions (docs/graph/gap/investigation.md "Prompts").

Each prompt is ``prompts/<name>.md`` (the system text of its gateway role) with a revision here. The gateway records a
call under ``gap.<name>.<revision>`` (``gateway_prompt``), which names its role (``gap_<name>``). The prompt versions of
a run are part of every site's input hash, so a revision change re-investigates on the next ``full`` run.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from typing import Final, Literal

PromptName = Literal["relevance", "investigate", "judge", "edits", "reconcile", "ai_answer"]

REVISIONS: Final[dict[PromptName, int]] = {
    "relevance": 1,
    "investigate": 1,
    "judge": 1,
    "edits": 1,
    "reconcile": 2,
    "ai_answer": 1,
}
"""The revision of each prompt; raise it whenever the prompt's text or its input format changes."""


def gateway_prompt(name: PromptName) -> str:
    """The prompt name the gateway records a call under: ``gap.<name>.<revision>``."""

    return f"gap.{name}.{REVISIONS[name]}"


@cache
def prompt_text(name: PromptName) -> str:
    """The system text of a prompt (``prompts/<name>.md``)."""

    return (files("graph.gap") / "prompts" / f"{name}.md").read_text(encoding="utf-8")


RELEVANCE_PROMPT: Final = gateway_prompt("relevance")
INVESTIGATE_PROMPT: Final = gateway_prompt("investigate")
REPAIR_PROMPT: Final = f"{INVESTIGATE_PROMPT}.repair"
"""The repair call of a site's check: role ``gap_investigate``, same system text, one corrected report."""
JUDGE_PROMPT: Final = gateway_prompt("judge")
EDITS_PROMPT: Final = gateway_prompt("edits")
"""``preview_edits`` for own text: role ``gap_edits``, the person's answer turned into edits (answers.md)."""
RECONCILE_PROMPT: Final = f"{JUDGE_PROMPT}.reconcile.{REVISIONS['reconcile']}"
"""Reconcile's same-meaning question (reconcile.md "The four questions"): role ``gap_judge``, its own system text
(``prompts/reconcile.md``) and revision; not part of a run's prompt versions (reconcile runs in the corpus reader)."""
AI_ANSWER_PROMPT: Final = f"{JUDGE_PROMPT}.ai_answer.{REVISIONS['ai_answer']}"
"""The second check of an AI answer (investigation.md "Second check", answers.md "Gates"): role ``gap_judge``, its own
system text (``prompts/ai_answer.md``) and revision; it sees the witness, the recommended option's basis quotes and
its edits."""
