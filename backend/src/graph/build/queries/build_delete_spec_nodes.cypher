// Persist: delete spec nodes in a batch.
UNWIND $rows AS row
MATCH (n:SpecNode {group_id: $project_id, uuid: row.uuid})
DETACH DELETE n
RETURN count(*) AS written
