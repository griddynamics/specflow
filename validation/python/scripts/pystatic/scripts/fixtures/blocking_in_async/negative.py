"""Fixture: clean under blocking_in_async -- wrapped, or inside a nested def,
or not a banned call at all."""

from __future__ import annotations

import asyncio

import pandas as pd


async def load_wrapped(path: str) -> object:
    return await asyncio.to_thread(pd.read_csv, path)


async def load_via_nested_def(path: str) -> object:
    def _sync_read() -> object:
        return pd.read_csv(path)  # a nested def's own body -- not this walk's scope

    return _sync_read()


async def compute(x: int) -> int:
    return x + 1
