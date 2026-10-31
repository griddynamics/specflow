"""QA commands of the MCP server (docs/mcp-server/testing.md): seed, QA user tokens, dev tokens.

Run from backend/ with the local stack up (`make up WITH=keycloak`):

    uv run --env-file ../.env.local python examples/mcp_qa.py seed [--mode keycloak|dev]
    uv run --env-file ../.env.local python examples/mcp_qa.py token alice [--client mcp|web]
    uv run --env-file ../.env.local python examples/mcp_qa.py dev-token alice@example.com [--client mcp|web] [--headers]

`seed` prints ids and counts. `token` and `dev-token` are the only commands that print a token (nothing else on the
line), so `TOKEN=$(... token alice)` works; `dev-token --headers` prints the `headersHelper` JSON instead. Passwords,
secrets and emails are never printed.
"""

from __future__ import annotations

import argparse
import os

from example_lib import run_example
from mcp_qa_lib import (
    QA_USERS,
    QaError,
    apply_local_defaults,
    dev_token,
    headers_json,
    require_env,
    seed,
    user_token,
)


def _user_key(user: str) -> str:
    """The QA user key for `alice` or her email."""

    key = next((name for name, email in QA_USERS.items() if user in (name, email)), None)
    if key is None:
        raise SystemExit("unknown QA user (expected one of: " + ", ".join(QA_USERS) + ")")
    return key


async def seed_command(args: argparse.Namespace) -> int:
    report = await seed(args.mode)
    print("\n".join(report.lines()))
    return 0


async def token_command(args: argparse.Namespace) -> int:
    names = ("KEYCLOAK_QA_PASSWORD", *(("KEYCLOAK_WEB_CLIENT_SECRET",) if args.client == "web" else ()))
    secrets_ = require_env(*names)
    try:
        token = await user_token(
            _user_key(args.user),
            secrets_["KEYCLOAK_QA_PASSWORD"],
            client=args.client,
            web_secret=secrets_.get("KEYCLOAK_WEB_CLIENT_SECRET"),
        )
    except QaError as error:
        raise SystemExit(str(error)) from None
    print(token)
    return 0


async def dev_token_command(args: argparse.Namespace) -> int:
    signing_key = require_env("AUTH_DEV_SIGNING_KEY")["AUTH_DEV_SIGNING_KEY"]
    token = dev_token(args.email, signing_key, client=args.client)
    print(headers_json(token) if args.headers else token)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)

    seeding = commands.add_parser("seed", help="idempotent seed of Keycloak, Postgres and Neo4j; prints ids and counts")
    seeding.add_argument("--mode", choices=("keycloak", "dev"), default="keycloak", help="subject kind of the users")
    seeding.set_defaults(handler=seed_command)

    token = commands.add_parser("token", help="print the access token of a QA user (authorization code with PKCE)")
    token.add_argument("user", help="alice, bob or carol (or the email)")
    token.add_argument("--client", choices=("mcp", "web"), default="mcp")
    token.set_defaults(handler=token_command)

    dev = commands.add_parser("dev-token", help="print an HS256 dev token (needs AUTH_DEV_SIGNING_KEY; works offline)")
    dev.add_argument("email")
    dev.add_argument("--client", choices=("mcp", "web"), default="mcp")
    dev.add_argument("--headers", action="store_true", help="print the headersHelper JSON instead of the token")
    dev.set_defaults(handler=dev_token_command)
    return parser.parse_args()


async def main() -> int:
    apply_local_defaults(os.environ)
    args = parse_args()
    return int(await args.handler(args))


if __name__ == "__main__":
    raise SystemExit(run_example(main))
