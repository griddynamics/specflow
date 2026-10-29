from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from llm.retry_policy import Failure


class ProgressDetector(Protocol):
    """Capture state and decide whether a failed attempt saved progress."""

    async def snapshot(self) -> object | None: ...

    def saved_progress(
        self,
        before: object | None,
        after: object | None,
        failure: Failure,
    ) -> bool | Awaitable[bool]: ...


@dataclass(frozen=True, slots=True)
class GitHeadProgressDetector:
    """Detect progress from Git HEAD changes or observed tool use."""

    cwd: Path
    min_tool_uses: int = 20

    def __post_init__(self) -> None:
        if isinstance(self.min_tool_uses, bool) or not isinstance(self.min_tool_uses, int):
            raise TypeError("min_tool_uses must be an integer")
        if self.min_tool_uses < 0:
            raise ValueError("min_tool_uses must not be negative")

    async def snapshot(self) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self.cwd),
                "rev-parse",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        stdout, _ = await process.communicate()
        if process.returncode:
            return None
        value = stdout.decode().strip()
        return value or None

    def saved_progress(
        self,
        before: object | None,
        after: object | None,
        failure: Failure,
    ) -> bool:
        head_advanced = bool(before and after and before != after)
        progress = getattr(failure, "progress", None)
        tool_uses = getattr(progress, "num_tool_uses", 0)
        return head_advanced or tool_uses >= self.min_tool_uses
