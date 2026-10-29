// Repair: restore the source fragment ids of edges addressed by element id.
UNWIND $rows AS row
MATCH ()-[r]->()
WHERE elementId(r) = row.id AND r.group_id = $project_id AND type(r) IN $relations
SET r.source_fragment_ids = row.fragment_ids
RETURN count(r) AS written
