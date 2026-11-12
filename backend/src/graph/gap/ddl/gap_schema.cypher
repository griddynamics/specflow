// The constraints and indexes of the graph.gap data model; discovered and applied by graph.core.ensure_schema.

CREATE CONSTRAINT graph_gap_finding_id IF NOT EXISTS FOR (n:Finding) REQUIRE (n.group_id, n.finding_id) IS UNIQUE;
CREATE CONSTRAINT graph_foreign_uuid_finding IF NOT EXISTS FOR (n:Finding) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
CREATE CONSTRAINT graph_foreign_uuid_externalref IF NOT EXISTS FOR (n:ExternalRef) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
CREATE INDEX graph_gap_finding_status IF NOT EXISTS FOR (n:Finding) ON (n.group_id, n.status);
CREATE INDEX graph_gap_finding_dedupe IF NOT EXISTS FOR (n:Finding) ON (n.group_id, n.dedupe_key);
CREATE INDEX graph_edge_key_about IF NOT EXISTS FOR ()-[r:ABOUT]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_refers_to IF NOT EXISTS FOR ()-[r:REFERS_TO]-() ON (r.group_id, r.edge_key);
