"""Fixtures for semgrep/tests.yml, named to match the config's own stem so
`semgrep --test` discovers it (`--test` ignores `paths:`, so `paths.include` itself is not
exercised here)."""

import asyncio
import time


async def waits_for_a_duration() -> None:
    # ruleid: kit.tests.no-sleep-literal
    await asyncio.sleep(0.15)
    # ruleid: kit.tests.no-sleep-literal
    await asyncio.sleep(1)
    # ruleid: kit.tests.no-sleep-literal
    time.sleep(0.25)


async def yields_to_the_loop(ready: asyncio.Event) -> None:
    # ok: kit.tests.no-sleep-literal
    await asyncio.sleep(0)
    # ok: kit.tests.no-sleep-literal
    await ready.wait()
