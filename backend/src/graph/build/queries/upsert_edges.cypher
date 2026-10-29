// Persist: MERGE edges of one relation by row.edge_key between the nodes with uuids row.source and row.target.
UNWIND $rows AS row
MATCH (s:SpecNode {group_id: $project_id, uuid: row.source})
MATCH (t:SpecNode {group_id: $project_id, uuid: row.target})
MERGE (s)-[r:__TYPE__ {group_id: $project_id, edge_key: row.edge_key}]->(t)
SET r += row.props
RETURN count(r) AS written
