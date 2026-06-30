#!/usr/bin/env python3
"""Resolve the authenticated GitHub login from the token and print it to stdout.

``specflow-init.sh`` uses this to default ``GIT_USER_NAME_DEFAULT`` / ``GITHUB_ORG``
to the token owner's real GitHub login (GET /user -> ``login``), which is the
correct repository owner. ``git config user.name`` is only a display name and not
a valid GitHub namespace, so it is the script's last-resort fallback, not this.

Reads ``GITHUB_TOKEN_DEFAULT`` (alias ``GITHUB_TOKEN``) from Settings. Prints the
login on success; on failure prints nothing to stdout and exits non-zero so the
caller can fall back to git config or surface a clean, actionable message.
"""

import asyncio
import sys

import httpx

from app.core.config import settings

_GITHUB_API_USER_URL = "https://api.github.com/user"


async def _resolve() -> int:
    token = settings.GITHUB_TOKEN_DEFAULT
    if not token:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(_GITHUB_API_USER_URL, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"GitHub /user request failed: {exc}", file=sys.stderr)
            return 1

    login = response.json().get("login")
    if not login:
        print("GitHub /user returned no login", file=sys.stderr)
        return 1

    print(login)
    return 0


def main() -> None:
    sys.exit(asyncio.run(_resolve()))


if __name__ == "__main__":
    main()
