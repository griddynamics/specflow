// Persist: delete the edge row.uuid stored under row.edge_key (an edge without a uuid: by key).
UNWIND $rows AS row
MATCH ()-[r:__TYPE__ {group_id: $project_id, edge_key: row.edge_key}]->()
WHERE r.uuid = row.uuid OR r.uuid IS NULL
DELETE r
RETURN count(*) AS written
