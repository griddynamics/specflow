"""``specflow api``: uvicorn on ``0.0.0.0:$API_PORT`` with the central logger only."""

from __future__ import annotations

from ipaddress import IPv4Address

import uvicorn

from api.app import create_app
from settings import RoleSettings

HOST = str(IPv4Address(0))  # 0.0.0.0, every interface of the container


def run(settings: RoleSettings) -> int:
    """Serve until SIGTERM/SIGINT; a failed startup exits uvicorn's non-zero code."""

    config = uvicorn.Config(
        create_app(settings),
        host=HOST,
        port=settings.app.api_port,
        log_config=None,
        access_log=False,
        server_header=False,
        date_header=True,
    )
    uvicorn.Server(config).run()
    return 0
