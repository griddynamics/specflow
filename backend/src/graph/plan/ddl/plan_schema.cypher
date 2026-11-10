// The constraints and indexes of the graph.plan data model; discovered and applied by graph.core.ensure_schema.

CREATE CONSTRAINT graph_plan_id IF NOT EXISTS FOR (n:Plan) REQUIRE (n.group_id, n.plan_id) IS UNIQUE;
CREATE CONSTRAINT graph_foreign_uuid_plan IF NOT EXISTS FOR (n:Plan) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
CREATE CONSTRAINT graph_plan_area_id IF NOT EXISTS FOR (n:WorkArea) REQUIRE (n.group_id, n.area_id) IS UNIQUE;
CREATE CONSTRAINT graph_foreign_uuid_workarea IF NOT EXISTS FOR (n:WorkArea) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
CREATE CONSTRAINT graph_plan_story_id IF NOT EXISTS FOR (n:Story) REQUIRE (n.group_id, n.story_id) IS UNIQUE;
CREATE CONSTRAINT graph_foreign_uuid_story IF NOT EXISTS FOR (n:Story) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
CREATE CONSTRAINT graph_plan_task_id IF NOT EXISTS FOR (n:Task) REQUIRE (n.group_id, n.task_id) IS UNIQUE;
CREATE CONSTRAINT graph_foreign_uuid_task IF NOT EXISTS FOR (n:Task) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
CREATE INDEX graph_plan_task_status IF NOT EXISTS FOR (n:Task) ON (n.group_id, n.status);
CREATE INDEX graph_plan_task_story IF NOT EXISTS FOR (n:Task) ON (n.group_id, n.story_id);
CREATE INDEX graph_plan_story_area IF NOT EXISTS FOR (n:Story) ON (n.group_id, n.area_id);
CREATE INDEX graph_edge_key_in_area IF NOT EXISTS FOR ()-[r:IN_AREA]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_in_story IF NOT EXISTS FOR ()-[r:IN_STORY]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_implements IF NOT EXISTS FOR ()-[r:IMPLEMENTS]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_waits_for IF NOT EXISTS FOR ()-[r:WAITS_FOR]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_supersedes IF NOT EXISTS FOR ()-[r:SUPERSEDES]-() ON (r.group_id, r.edge_key);

// Plan link reads (plan_read_links) find one relationship type of a project by group_id alone; the
// (group_id, edge_key) indexes above do not serve that, so without these a read scans every tenant's links.
CREATE INDEX graph_edge_group_in_area IF NOT EXISTS FOR ()-[r:IN_AREA]-() ON (r.group_id);
CREATE INDEX graph_edge_group_in_story IF NOT EXISTS FOR ()-[r:IN_STORY]-() ON (r.group_id);
CREATE INDEX graph_edge_group_implements IF NOT EXISTS FOR ()-[r:IMPLEMENTS]-() ON (r.group_id);
CREATE INDEX graph_edge_group_waits_for IF NOT EXISTS FOR ()-[r:WAITS_FOR]-() ON (r.group_id);
CREATE INDEX graph_edge_group_supersedes IF NOT EXISTS FOR ()-[r:SUPERSEDES]-() ON (r.group_id);
