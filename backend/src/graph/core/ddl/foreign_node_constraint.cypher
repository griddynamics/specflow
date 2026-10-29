// Uniqueness constraint of one foreign label (a declared label of another owner than build, from a
// submodule ontology file): the (group_id, uuid) key upsert_foreign_node.cypher MERGEs on.
// Template, not part of CORE_DDL: the shipped ontology has no foreign label. A submodule's DDL holds
// this statement per foreign label, rendered by graph.core.queries.foreign_node_constraint
// (__FOREIGN_LABEL__ the label, __FOREIGN_NAME__ the label in lower case); without it schema setup
// fails with schema_ddl_incomplete (graph.core.schema).
CREATE CONSTRAINT graph_foreign_uuid___FOREIGN_NAME__ IF NOT EXISTS
  FOR (n:__FOREIGN_LABEL__) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
