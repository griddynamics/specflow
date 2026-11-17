"""Gap queries: every query the gap run, the gap service and the migration of first-slice findings run themselves, as
named ``Query`` objects (like the plan's).

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

READ_GAPS: Final = Query("gap_read_gaps", "gap.gap_read_gaps")
"""Gap nodes by status (and id) with their ABOUT, IMPACTS, CITES, FOLLOWS_UP relations and clarifications."""

READ_FIRST_SLICE: Final = Query("gap_read_first_slice", "gap.gap_read_first_slice")
"""First-slice ``Finding`` nodes with their ABOUT ends and ExternalRefs: read by the migration of first-slice findings
only."""

DELETE_FIRST_SLICE_FINDINGS: Final = Query("gap_delete_first_slice_findings", "gap.gap_delete_first_slice_findings")
"""Delete the project's first-slice ``Finding`` nodes (the last step of the migration)."""

DELETE_FIRST_SLICE_REFS: Final = Query("gap_delete_first_slice_refs", "gap.gap_delete_first_slice_refs")
"""Delete the project's first-slice ``ExternalRef`` nodes (the last step of the migration)."""

GAP_DDL: Final = read_ddl_file(_DDL_FILE)
