"""``specflow migrate``: ``alembic upgrade head`` with the shipped alembic.ini.

The config file is, in order: ``ALEMBIC_CONFIG`` (Alembic's own variable),
then the ``alembic.ini`` of the source layout this package was loaded from
(``<root>/src/db/migrate.py`` -> ``<root>/alembic.ini``), then
``./alembic.ini``. The image (S11) keeps ``/app/alembic.ini`` and
``/app/migrations/`` with ``WORKDIR /app``.
``alembic.ini`` resolves ``script_location`` next to itself. A second run is a
no-op: Alembic applies only revisions newer than the stamped one.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from logger.logger import INFO, get_logger, log_event
from settings import DatabaseSettings, RoleSettings

logger = get_logger("db.migrate")

CONFIG_VARIABLE: Final = "ALEMBIC_CONFIG"
_SOURCE_ROOT: Final = Path(__file__).resolve().parents[2]


def find_alembic_ini(environ: Mapping[str, str] | None = None) -> Path:
    """Return the alembic.ini to use; raise ``FileNotFoundError`` naming the variable."""

    source: Mapping[str, str] = os.environ if environ is None else environ
    override = source.get(CONFIG_VARIABLE, "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"{CONFIG_VARIABLE} does not point at a file")
        return path
    for candidate in (_SOURCE_ROOT / "alembic.ini", Path.cwd() / "alembic.ini"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"alembic.ini not found; set {CONFIG_VARIABLE}")


def build_config(settings: RoleSettings, ini: Path | None = None) -> Config:
    """An Alembic config bound to the role's database and agent role name."""

    cfg = Config(str(ini or find_alembic_ini()))
    cfg.attributes["database_url"] = settings.database.url()
    if settings.agent_database is not None:
        cfg.attributes["agent_user"] = settings.agent_database.user
    return cfg


async def current_revision(database: DatabaseSettings) -> str | None:
    """The stamped revision of the database, or ``None`` when unstamped."""

    engine = create_async_engine(database.url())
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(lambda sync: MigrationContext.configure(sync).get_current_revision())
    finally:
        await engine.dispose()


def run(settings: RoleSettings) -> int:
    """Upgrade to head and log the revision before and after."""

    cfg = build_config(settings)
    before = asyncio.run(current_revision(settings.database))
    command.upgrade(cfg, "head")
    after = asyncio.run(current_revision(settings.database))
    log_event(
        logger,
        INFO,
        "db.migration.completed",
        revision_before=before,
        revision_after=after,
        changed=before != after,
    )
    return 0
