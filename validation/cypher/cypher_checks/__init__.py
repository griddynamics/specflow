"""`cypher_checks`: static and live validation of `.cypher` query files against a schema file.

`lint` reads query text alone; `cyver_check` and `explain` run against a throwaway Neo4j
seeded with the schema's fixture graph; `no_inline_queries` keeps query text out of Python.
"""

from __future__ import annotations
