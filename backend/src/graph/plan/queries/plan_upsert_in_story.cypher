// Persist: MERGE IN_STORY from a task to its home story.
UNWIND $rows AS row
MATCH (t:Task {group_id: $project_id, task_id: row.source})
MATCH (s:Story {group_id: $project_id, story_id: row.target})
MERGE (t)-[r:IN_STORY {group_id: $project_id, edge_key: row.key}]->(s)
RETURN count(r) AS written
