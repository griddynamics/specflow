"""``SlackChannels``: the Slack channel a project is bound to and the gap message posted there for each gap
(docs/slack/gap-answering.md "The channel flow").

A project member connects the channel from inside it (``/specflow connect <project id>``); every open question of the
project is then posted there once, and a reply in its thread answers it. A project has one channel and a channel one
project. The store maps both ways: project to channel, and a message (channel and ``ts``, the thread a reply names) to
its gap.

Spike storage: a JSON file in the graph state folder (``GRAPH_STATE_DIR``), as ``SlackLinks``; phase 1b moves it to
tables.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

CHANNELS_FILE: Final = "slack-channels.json"


@dataclass(frozen=True)
class Binding:
    project_id: str
    channel_id: str
    channel_name: str
    bound_by: str
    private: bool = True


@dataclass(frozen=True)
class Posted:
    project_id: str
    gap_id: str
    channel_id: str
    ts: str
    number: int = 0  # the question's number in its channel: #1, #2, ...


class ChannelTakenError(Exception):
    """The channel is connected to another project."""


class SlackChannels:
    """Bindings and posted gap messages, in one JSON file."""

    def __init__(self, folder: Path) -> None:
        self._path = folder / CHANNELS_FILE
        self._lock = asyncio.Lock()

    async def bind(self, binding: Binding) -> Binding | None:
        """Bind the project to the channel and return its earlier binding. ``ChannelTakenError`` when another project
        has the channel. The same channel again changes nothing; another channel replaces the earlier one, and the
        gaps posted there are forgotten, so the new channel gets every open question."""

        async with self._lock:
            data = await asyncio.to_thread(self._read)
            bindings = data["bindings"]
            if any(
                pid != binding.project_id and b.get("channel_id") == binding.channel_id for pid, b in bindings.items()
            ):
                raise ChannelTakenError
            earlier = _binding(binding.project_id, bindings.get(binding.project_id))
            if earlier is not None and earlier.channel_id == binding.channel_id:
                return earlier
            bindings[binding.project_id] = {
                "channel_id": binding.channel_id,
                "channel_name": binding.channel_name,
                "bound_by": binding.bound_by,
                "private": binding.private,
            }
            data["posts"] = {key: post for key, post in data["posts"].items() if post["p"] != binding.project_id}
            await asyncio.to_thread(self._write, data)
            return earlier

    async def unbind(self, project_id: str) -> None:
        async with self._lock:
            data = await asyncio.to_thread(self._read)
            data["bindings"].pop(project_id, None)
            data["posts"] = {key: post for key, post in data["posts"].items() if post["p"] != project_id}
            await asyncio.to_thread(self._write, data)

    async def binding(self, project_id: str) -> Binding | None:
        data = await self._snapshot()
        return _binding(project_id, data["bindings"].get(project_id))

    async def bindings(self) -> list[Binding]:
        """Every binding, by project id."""

        data = await self._snapshot()
        found = [_binding(project_id, bound) for project_id, bound in sorted(data["bindings"].items())]
        return [binding for binding in found if binding is not None]

    async def project_of(self, channel_id: str) -> str | None:
        """The project bound to ``channel_id``; ``None`` when none is."""

        data = await self._snapshot()
        found = [pid for pid, bound in data["bindings"].items() if bound.get("channel_id") == channel_id]
        return str(found[0]) if len(found) == 1 else None

    async def add_post(self, posted: Posted) -> None:
        async with self._lock:
            data = await asyncio.to_thread(self._read)
            data["posts"][f"{posted.channel_id}:{posted.ts}"] = {
                "p": posted.project_id,
                "g": posted.gap_id,
                "n": posted.number,
            }
            await asyncio.to_thread(self._write, data)

    async def posted_gaps(self, project_id: str) -> dict[str, Posted]:
        """The gap messages of the project's bound channel, by gap id."""

        data = await self._snapshot()
        found: dict[str, Posted] = {}
        for key, post in data["posts"].items():
            if post["p"] == project_id:
                channel_id, ts = key.split(":", 1)
                found[str(post["g"])] = Posted(project_id, str(post["g"]), channel_id, ts, int(post.get("n", 0)))
        return found

    async def thread(self, channel_id: str, thread_ts: str) -> Posted | None:
        """The gap whose message started the thread ``thread_ts`` in ``channel_id``; ``None`` for any other thread."""

        data = await self._snapshot()
        post = data["posts"].get(f"{channel_id}:{thread_ts}")
        if post is None:
            return None
        return Posted(str(post["p"]), str(post["g"]), channel_id, thread_ts, int(post.get("n", 0)))

    async def _snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._read)

    def _read(self) -> dict[str, Any]:
        data: Any = json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else {}
        data = data if isinstance(data, dict) else {}
        return {"bindings": dict(data.get("bindings", {})), "posts": dict(data.get("posts", {}))}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self._path)


def _binding(project_id: str, found: dict[str, Any] | None) -> Binding | None:
    if found is None:
        return None
    channel_id, name, by = str(found["channel_id"]), str(found["channel_name"]), str(found["bound_by"])
    return Binding(project_id, channel_id, name, by, bool(found.get("private", True)))
