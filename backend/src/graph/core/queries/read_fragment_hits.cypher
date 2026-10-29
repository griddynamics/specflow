// Fragments by id with their document.
MATCH (f:Fragment)
WHERE f.group_id = $project_id AND f.fragment_id IN $fragment_ids
OPTIONAL MATCH (d:Document {group_id: $project_id, document_id: f.document_id})
RETURN properties(f) AS props, properties(d) AS document
