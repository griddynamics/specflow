// First-slice findings of the project, read only by the migration of first-slice findings: the stored properties of
// each Finding, the spec ids and the fragment ids its ABOUT relations end at, and the type and value of each
// ExternalRef it REFERS_TO.
MATCH (f:Finding {group_id: $project_id})
OPTIONAL MATCH (f)-[:ABOUT {group_id: $project_id}]->(n:SpecNode {group_id: $project_id})
WITH f, collect(DISTINCT n.spec_id) AS spec_ids
OPTIONAL MATCH (f)-[:ABOUT {group_id: $project_id}]->(e:Fragment {group_id: $project_id})
WITH f, spec_ids, collect(DISTINCT e.fragment_id) AS fragment_ids
OPTIONAL MATCH (f)-[:REFERS_TO {group_id: $project_id}]->(x:ExternalRef {group_id: $project_id})
WITH f, spec_ids, fragment_ids, collect(CASE WHEN x IS NULL THEN NULL ELSE {type: x.type, value: x.value} END) AS refs
RETURN properties(f) AS finding, spec_ids, fragment_ids, refs
ORDER BY f.finding_id
