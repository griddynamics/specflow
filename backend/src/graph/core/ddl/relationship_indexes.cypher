// Edge-key range indexes, one per relation of the shipped ontology (src/graph/core/ontology/spec-ontology.yaml).
// Schema setup, unscoped: applied by graph.core.schema.ensure_schema, one statement per object.
// Owned writes find a relationship by (group_id, edge_key) with an index seek. A relation the
// ontology declares without its statement here fails setup with schema_ddl_incomplete.
CREATE INDEX graph_edge_key_grouped_by IF NOT EXISTS FOR ()-[r:GROUPED_BY]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_integrates_with IF NOT EXISTS FOR ()-[r:INTEGRATES_WITH]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_part_of IF NOT EXISTS FOR ()-[r:PART_OF]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_realized_by IF NOT EXISTS FOR ()-[r:REALIZED_BY]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_relates_to IF NOT EXISTS FOR ()-[r:RELATES_TO]-() ON (r.group_id, r.edge_key);
