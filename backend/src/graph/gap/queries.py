"""Gap queries: every query the findings library runs itself, as named ``Query`` objects (like the plan's).

The canonical statements live in ``.cypher`` files under ``queries/`` next to this module, the SQL ports under
``graph/stores/postgres/queries/gap/``. Nodes and edges are written through ``OwnedWriter`` (core queries). Every query
binds the project id. The constraints and indexes are ``ddl/gap_schema.cypher``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from graph.core.queries import Query
from graph.core.query_files import read_ddl_file

_DDL_FILE: Final = Path(__file__).resolve().parent / "ddl" / "gap_schema.cypher"

READ_FINDINGS: Final = Query("gap_read_findings", "gap.gap_read_findings")

SET_OPEN_FINDING: Final = Query("gap_set_open_finding", "gap.gap_set_open_finding")

DELETE_OPEN_FINDING: Final = Query("gap_delete_open_finding", "gap.gap_delete_open_finding")

DELETE_UNUSED_REFS: Final = Query("gap_delete_unused_refs", "gap.gap_delete_unused_refs")

GAP_DDL: Final = read_ddl_file(_DDL_FILE)
