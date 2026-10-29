// Persist: create spec nodes in a batch; the domain label is set on creation only.
UNWIND $rows AS row
MERGE (n:SpecNode {group_id: $project_id, uuid: row.uuid})
ON CREATE SET n:$(row.label)
SET n += row.props
RETURN count(n) AS written
