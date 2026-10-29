"""Fixture: a direct blocking call inside an async def, unwrapped."""

from __future__ import annotations

import pandas as pd


async def load(path: str) -> object:
    return pd.read_csv(path)
