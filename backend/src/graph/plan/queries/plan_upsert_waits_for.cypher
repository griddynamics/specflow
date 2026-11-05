// Persist: MERGE WAITS_FOR between tasks, with its source (planner or stitch).
UNWIND $rows AS row
MATCH (w:Task {group_id: $project_id, task_id: row.source})
MATCH (p:Task {group_id: $project_id, task_id: row.target})
MERGE (w)-[r:WAITS_FOR {group_id: $project_id, edge_key: row.key}]->(p)
SET r += row.properties
RETURN count(r) AS written
