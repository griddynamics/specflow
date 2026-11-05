// Persist: MERGE IN_AREA from a spec node (by uuid) to its work area, with the placement rule and edges.
UNWIND $rows AS row
MATCH (s:SpecNode {group_id: $project_id, uuid: row.source})
MATCH (a:WorkArea {group_id: $project_id, area_id: row.target})
MERGE (s)-[r:IN_AREA {group_id: $project_id, edge_key: row.key}]->(a)
SET r += row.properties
RETURN count(r) AS written
