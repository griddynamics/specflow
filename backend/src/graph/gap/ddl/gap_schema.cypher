// The constraints and indexes of the graph.gap data model; discovered and applied by graph.core.ensure_schema.

// Gap and Clarification (docs/graph/gap/data-model.md "Constraints and indexes").
CREATE CONSTRAINT graph_gap_id IF NOT EXISTS FOR (n:Gap) REQUIRE (n.group_id, n.gap_id) IS UNIQUE;
CREATE CONSTRAINT graph_foreign_uuid_gap IF NOT EXISTS FOR (n:Gap) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
CREATE CONSTRAINT graph_clarification_id IF NOT EXISTS FOR (n:Clarification) REQUIRE (n.group_id, n.clr_id) IS UNIQUE;
CREATE CONSTRAINT graph_foreign_uuid_clarification IF NOT EXISTS FOR (n:Clarification) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
CREATE INDEX graph_gap_status IF NOT EXISTS FOR (n:Gap) ON (n.group_id, n.status);
CREATE INDEX graph_gap_signature IF NOT EXISTS FOR (n:Gap) ON (n.group_id, n.signature);
CREATE INDEX graph_edge_key_about IF NOT EXISTS FOR ()-[r:ABOUT]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_impacts IF NOT EXISTS FOR ()-[r:IMPACTS]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_cites IF NOT EXISTS FOR ()-[r:CITES]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_follows_up IF NOT EXISTS FOR ()-[r:FOLLOWS_UP]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_answers IF NOT EXISTS FOR ()-[r:ANSWERS]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_placed_in IF NOT EXISTS FOR ()-[r:PLACED_IN]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_grounded_in IF NOT EXISTS FOR ()-[r:GROUNDED_IN]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_replaces IF NOT EXISTS FOR ()-[r:REPLACES]-() ON (r.group_id, r.edge_key);
