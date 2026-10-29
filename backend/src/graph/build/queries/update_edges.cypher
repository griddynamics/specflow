// Persist: SET += row.props on the edge row.uuid stored under one of row.keys (an edge without a uuid is matched on its key alone).
UNWIND $rows AS row
MATCH ()-[r:__TYPE__ {group_id: $project_id}]->()
WHERE r.edge_key IN row.keys AND (r.uuid = row.uuid OR r.uuid IS NULL)
SET r += row.props
RETURN count(r) AS written
