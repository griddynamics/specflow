"""Tier 2: a removed Drive folder can be connected again (FR-SRC-0016.AC4).

``SPECFLOW_TEST_POSTGRES=1``; see specflow_db_live_support.
"""

from __future__ import annotations

from db import sources as repo
from db.removal import remove_source
from specflow_live import requires_live
from specflow_sources_live_support import make_drive
from specflow_sources_write_support import source_exists, tx

pytestmark = requires_live("POSTGRES")

FOLDER = "readd-folder-x"
LINK = f"https://drive.google.com/drive/folders/{FOLDER}"


async def test_a_folder_removed_as_a_source_can_be_connected_again(sources_world) -> None:
    # FR-SRC-0016.AC4: the unique (project, kind, folder) key must not outlive the source.
    # Cleanup: the rows live in a per-test scratch database (specflow_pytest_<hex>) that
    # the fixture drops afterwards.
    w, p = sources_world, sources_world.project_a
    first = await tx(w, lambda s: make_drive(s, p, w.user_id, folder=FOLDER))

    async def create(s):  # the save's own DB path
        return await repo.create_connection(
            s, project_id=p, kind="gdrive", external_id=FOLDER, link=LINK, name="Folder", connected_by=w.user_id
        )

    _, created = await tx(w, create)
    assert created is False  # control: while it is a source, the folder is taken

    await tx(w, lambda s: remove_source(s, p, first))
    assert not await tx(w, lambda s: source_exists(s, first))

    row, created = await tx(w, create)
    assert created is True and row.id != first and row.external_id == FOLDER
    assert await tx(w, lambda s: source_exists(s, row.id))
