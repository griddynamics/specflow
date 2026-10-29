// Repair: delete build-owned edges addressed by element id.
UNWIND $ids AS id
MATCH ()-[r]->()
WHERE elementId(r) = id AND r.group_id = $project_id AND type(r) IN $relations
DELETE r
RETURN count(*) AS written
