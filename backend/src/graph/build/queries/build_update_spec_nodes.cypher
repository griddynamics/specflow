// Persist: update the properties of existing spec nodes in a batch.
UNWIND $rows AS row
MATCH (n:SpecNode {group_id: $project_id, uuid: row.uuid})
SET n += row.props
RETURN count(n) AS written
