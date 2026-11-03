// Persist: MERGE SUPERSEDES from a new task revision to the task it replaces.
UNWIND $rows AS row
MATCH (n:Task {group_id: $project_id, task_id: row.source})
MATCH (o:Task {group_id: $project_id, task_id: row.target})
MERGE (n)-[r:SUPERSEDES {group_id: $project_id, edge_key: row.key}]->(o)
RETURN count(r) AS written
