"""The prompts of ``graph.gap`` and their versions (docs/graph/gap/investigation.md "Prompts").

Each prompt is ``prompts/<name>.md`` (the system text of its gateway role) with a revision here. The gateway records a
call under ``gap.<name>.<revision>`` (``gateway_prompt``), which names its role (``gap_<name>``). The prompt versions of
a run are part of every site's input hash, so a revision change re-investigates on the next ``full`` run.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from typing import Final, Literal

PromptName = Literal["relevance", "investigate", "judge", "edits"]

REVISIONS: Final[dict[PromptName, int]] = {"relevance": 1, "investigate": 1, "judge": 1, "edits": 1}
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
"""The repair call of the check step: role ``gap_investigate``, same system text, one corrected report."""
JUDGE_PROMPT: Final = gateway_prompt("judge")
