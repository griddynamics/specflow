// Persist: MERGE IMPLEMENTS from a story to its anchor spec node (by uuid).
UNWIND $rows AS row
MATCH (s:Story {group_id: $project_id, story_id: row.source})
MATCH (n:SpecNode {group_id: $project_id, uuid: row.target})
MERGE (s)-[r:IMPLEMENTS {group_id: $project_id, edge_key: row.key}]->(n)
RETURN count(r) AS written
