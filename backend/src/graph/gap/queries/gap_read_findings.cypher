// Findings: the stored properties and the references of each, filtered by status and, when given, by id, author, dedupe
// key and about ids (an empty $spec_ids list or a null filter asks for all). Sorting and paging are the library's.
MATCH (f:Finding {group_id: $project_id})
WHERE f.status IN $statuses
  AND ($finding_id IS NULL OR f.finding_id = $finding_id)
  AND ($created_by IS NULL OR f.created_by = $created_by)
  AND ($dedupe_key IS NULL OR f.dedupe_key = $dedupe_key)
  AND (size($spec_ids) = 0 OR any(spec_id IN coalesce(f.about_spec_ids, []) WHERE spec_id IN $spec_ids))
OPTIONAL MATCH (f)-[:REFERS_TO {group_id: $project_id}]->(x:ExternalRef {group_id: $project_id})
WITH f, collect(CASE WHEN x IS NULL THEN NULL ELSE [x.type, x.value] END) AS refs
RETURN properties(f) AS finding, refs
ORDER BY f.finding_id
