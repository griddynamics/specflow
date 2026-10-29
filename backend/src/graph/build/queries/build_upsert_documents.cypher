// Persist: create or update documents in a batch.
UNWIND $rows AS row
MERGE (d:Document {group_id: $project_id, document_id: row.document_id})
SET d += row.props, d.last_build_id = $build_id
RETURN count(d) AS written
