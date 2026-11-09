// Persist: MERGE IMPLEMENTS from a task to a spec node (by uuid).
UNWIND $rows AS row
MATCH (t:Task {group_id: $project_id, task_id: row.source})
MATCH (n:SpecNode {group_id: $project_id, uuid: row.target})
MERGE (t)-[r:IMPLEMENTS {group_id: $project_id, edge_key: row.key}]->(n)
RETURN count(r) AS written
