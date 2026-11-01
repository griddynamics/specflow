"""The Cypher DDL of the gap test ontology, for the offline schema tests (``test_graph_schema``, ``test_graph_writer``).

Cypher text is the subject of those tests (DDL parsing of the Bolt stores), so it lives here and not in the
backend-neutral ``graph_support``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from graph.core.queries import CORE_DDL
from graph.core.query_files import read_ddl_file

# Schema DDL of ``GAP_ONTOLOGY``: a submodule ontology brings the DDL of what it declares, the edge-key
# range index of each of its relations and the (group_id, uuid) constraint of each foreign label.
GAP_DDL_TEXT: Final = """// Schema DDL of the gap test ontology (applied after the shipped DDL).
CREATE INDEX graph_edge_key_cites IF NOT EXISTS FOR ()-[r:CITES]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_flags IF NOT EXISTS FOR ()-[r:FLAGS]-() ON (r.group_id, r.edge_key);
CREATE CONSTRAINT graph_foreign_uuid_finding IF NOT EXISTS
  FOR (n:Finding) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
"""


def load_gap_ddl(directory: Path) -> tuple[str, ...]:
    """The shipped DDL statements followed by those of ``GAP_DDL_TEXT``, written into ``directory``."""

    path = Path(directory) / "gap-ddl.cypher"
    path.write_text(GAP_DDL_TEXT, encoding="utf-8")
    return (*CORE_DDL, *read_ddl_file(path))
