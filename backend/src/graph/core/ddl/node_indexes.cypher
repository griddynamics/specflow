// Node indexes of the spec graph (docs/graph/data-model.md "Constraints and indexes").
// Schema setup, unscoped: applied by graph.core.schema.ensure_schema, one statement per object.
// The vector dimension is the parameter $dimension (LLMConfig.embedding_dim); similarity is cosine.
CREATE INDEX graph_spec_node_identity_key IF NOT EXISTS FOR (n:SpecNode) ON (n.group_id, n.identity_key);
CREATE VECTOR INDEX graph_spec_node_embedding IF NOT EXISTS FOR (n:SpecNode) ON (n.embedding) OPTIONS {indexConfig: {`vector.dimensions`: $dimension, `vector.similarity_function`: 'cosine'}};
CREATE VECTOR INDEX graph_spec_node_name_embedding IF NOT EXISTS FOR (n:SpecNode) ON (n.name_embedding) OPTIONS {indexConfig: {`vector.dimensions`: $dimension, `vector.similarity_function`: 'cosine'}};
CREATE FULLTEXT INDEX graph_spec_node_text IF NOT EXISTS FOR (n:SpecNode) ON EACH [n.name, n.statement_text];
CREATE FULLTEXT INDEX graph_fragment_text IF NOT EXISTS FOR (n:Fragment) ON EACH [n.text];
