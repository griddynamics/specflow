from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from llm.agent import Agent, AgentInput
from llm.permissions import AgentPermissions
from llm.result import AgentResult
from llm.settings import AgentSettings
from llm.stream import MessageObserver


class CodingAgent:
    """Agent composition requiring explicit coding permissions.

    Callers may opt into ``conservative_coding_profile(cwd)`` and
    ``conservative_bash_guard_hooks()``; neither is applied implicitly.
    """

    def __init__(
        self,
        *,
        model: str,
        permissions: AgentPermissions,
        system_prompt: str | None = None,
        cwd: str | Path | None = None,
        timeout: float = 3600.0,
        message_observer: MessageObserver | None = None,
        settings: AgentSettings | None = None,
    ) -> None:
        if not isinstance(permissions, AgentPermissions):
            raise TypeError("permissions must be an AgentPermissions instance")
        permissions.validate_workspace(cwd)
        effective_settings = replace(
            settings or AgentSettings(),
            permissions=permissions,
        )
        self.agent = Agent(
            model=model,
            system_prompt=system_prompt,
            cwd=cwd,
            timeout=timeout,
            message_observer=message_observer,
            settings=effective_settings,
        )

    async def run(
        self,
        prompt_or_messages: AgentInput,
        *,
        resume_session_id: str | None = None,
        output_format: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        """Delegate one run to the composed general-purpose agent."""

        return await self.agent.run(
            prompt_or_messages,
            resume_session_id=resume_session_id,
            output_format=output_format,
        )
