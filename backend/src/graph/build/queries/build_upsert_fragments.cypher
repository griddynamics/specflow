// Persist: create or update fragments in a batch and link them to their document.
UNWIND $rows AS row
MATCH (d:Document {group_id: $project_id, document_id: row.document_id})
MERGE (f:Fragment {group_id: $project_id, fragment_id: row.fragment_id})
SET f += row.props, f.last_build_id = $build_id
MERGE (d)-[:CONTAINS {group_id: $project_id}]->(f)
RETURN count(f) AS written
