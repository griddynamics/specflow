#!/usr/bin/env python3
"""Resolve the P10Y organisation id from the API key and print it to stdout.

The organisation is bound to the API key, so it is read from Compass
GET /api/user/self (``organisationId``). Used by specflow-init.sh to verify P10Y
access and persist the org id to .env.

Reads P10Y_API_KEY from the Settings; the Compass base URL is the single in-code
default (P10Y_DEFAULT_BASE_URL). Prints the numeric organisation id on success;
on failure prints nothing to stdout and exits non-zero so the caller can surface
a clean, actionable message.
"""

import asyncio
import sys

from app.core.config import settings
from app.services.p10y.p10y_api_client import P10YInternalAPIClient


async def _resolve() -> int:
    if not settings.P10Y_API_KEY:
        print("P10Y_API_KEY is not set", file=sys.stderr)
        return 1

    client = P10YInternalAPIClient(
        base_url=settings.P10Y_BASE_URL,
        api_key=settings.P10Y_API_KEY,
    )
    try:
        user = await client.get_current_user()
        print(user.organisationId)
        return 0
    finally:
        await client.close()


def main() -> None:
    sys.exit(asyncio.run(_resolve()))


if __name__ == "__main__":
    main()
