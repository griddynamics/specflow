// Uniqueness constraints of the spec graph (docs/graph/data-model.md "Constraints and indexes").
// Schema setup, unscoped: applied by graph.core.schema.ensure_schema, one statement per object.
// Every key is composite with the tenant key group_id (= project_id), so a key is unique per project.
CREATE CONSTRAINT graph_spec_node_spec_id IF NOT EXISTS FOR (n:SpecNode) REQUIRE (n.group_id, n.spec_id) IS UNIQUE;
CREATE CONSTRAINT graph_spec_node_uuid IF NOT EXISTS FOR (n:SpecNode) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
CREATE CONSTRAINT graph_document_id IF NOT EXISTS FOR (n:Document) REQUIRE (n.group_id, n.document_id) IS UNIQUE;
CREATE CONSTRAINT graph_fragment_id IF NOT EXISTS FOR (n:Fragment) REQUIRE (n.group_id, n.fragment_id) IS UNIQUE;
