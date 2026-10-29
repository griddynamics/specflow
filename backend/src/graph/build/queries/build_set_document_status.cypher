// Persist: set the status of documents in a batch.
UNWIND $rows AS row
MATCH (d:Document {group_id: $project_id, document_id: row.document_id})
SET d.status = row.status, d.last_build_id = $build_id
RETURN count(d) AS written
